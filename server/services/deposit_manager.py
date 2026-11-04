"""Centralized deposit accountability and state machine manager."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from server.core.identity import IdentityService
from server.core.security import (
    PersistenceCorruptionError,
    StateTransitionError,
    atomic_write_json,
    safe_load_json,
    utc_now_iso,
    validate_crawler_id,
)
from server.schemas import (
    ALLOWED_PAYMENT_TRANSITIONS,
    ALLOWED_POLICY_DECISION_TRANSITIONS,
    ALLOWED_REFUND_TRANSITIONS,
    ChallengeRecord,
    DepositRecord,
    DepositResponse,
    DepositState,
    PaymentStatus,
    PolicyDecisionStatus,
    RefundTransactionStatus,
    SessionRecord,
    SessionStatus,
    SiteConfig,
)
from server.services.audit_logger import AuditLogger
from server.services.payment_service import (
    PaymentApprovalRequiredError,
    PaymentProvider,
    PaymentProviderError,
    RefundDetailsLookupResult,
    RefundExecutionResult,
)


class DepositManager:
    """
    Manages challenges, deposit records, payment verification, policy acceptance,
    and refund/forfeiture state machines with atomic persistence.
    """

    def __init__(
        self,
        payments_db_path: Path,
        site_config: SiteConfig,
        payment_provider: PaymentProvider,
        audit_logger: AuditLogger,
    ) -> None:
        self._db_path = payments_db_path
        self._site_config = site_config
        self._payment_provider = payment_provider
        self._audit = audit_logger
        self._lock = asyncio.Lock()
        self._challenges: dict[str, ChallengeRecord] = {}
        self._deposits: dict[str, DepositRecord] = {}
        self._active_refunds: set[str] = set()
        self._active_refund_reconciliations: set[str] = set()

    @property
    def payment_provider(self) -> PaymentProvider:
        return self._payment_provider

    def set_payment_provider(self, provider: PaymentProvider) -> None:
        """Override the payment provider (used in dependency injection and tests)."""
        self._payment_provider = provider

    def load_from_disk(self) -> None:
        """Load persisted challenges and deposits from disk and validate schemas."""
        raw = safe_load_json(
            self._db_path,
            expected_top_keys=("challenges", "deposits"),
        )
        loaded_challenges: dict[str, ChallengeRecord] = {}
        loaded_deposits: dict[str, DepositRecord] = {}

        for cid, cdata in raw["challenges"].items():
            try:
                crec = ChallengeRecord.model_validate(cdata)
            except ValidationError as exc:
                raise PersistenceCorruptionError(
                    f"Malformed ChallengeRecord {cid} in {self._db_path}: {exc}"
                ) from exc
            if crec.challenge_id != cid:
                raise PersistenceCorruptionError(
                    f"Mismatched challenge_id key {cid} != {crec.challenge_id}"
                )
            loaded_challenges[cid] = crec

        for did, ddata in raw["deposits"].items():
            try:
                drec = DepositRecord.model_validate(ddata)
            except ValidationError as exc:
                raise PersistenceCorruptionError(
                    f"Malformed DepositRecord {did} in {self._db_path}: {exc}"
                ) from exc
            if drec.deposit_id != did:
                raise PersistenceCorruptionError(
                    f"Mismatched deposit_id key {did} != {drec.deposit_id}"
                )
            loaded_deposits[did] = drec

        self._challenges = loaded_challenges
        self._deposits = loaded_deposits

    def _persist_unlocked(self) -> None:
        payload = {
            "challenges": {
                cid: rec.model_dump(mode="json")
                for cid, rec in self._challenges.items()
            },
            "deposits": {
                did: rec.model_dump(mode="json")
                for did, rec in self._deposits.items()
            },
        }
        atomic_write_json(self._db_path, payload)

    @staticmethod
    def _transition_payment(record: DepositRecord, target: PaymentStatus) -> None:
        allowed = ALLOWED_PAYMENT_TRANSITIONS.get(record.payment_status, set())
        if target not in allowed:
            raise StateTransitionError(
                f"Invalid payment_status transition from {record.payment_status.value} to {target.value}"
            )
        record.payment_status = target

    @staticmethod
    def _transition_decision(
        record: DepositRecord, target: PolicyDecisionStatus
    ) -> None:
        allowed = ALLOWED_POLICY_DECISION_TRANSITIONS.get(record.policy_decision, set())
        if target not in allowed:
            raise StateTransitionError(
                f"Invalid policy_decision transition from {record.policy_decision.value} to {target.value}"
            )
        record.policy_decision = target

    @staticmethod
    def _transition_refund(
        record: DepositRecord, target: RefundTransactionStatus
    ) -> None:
        allowed = ALLOWED_REFUND_TRANSITIONS.get(record.refund_status, set())
        if target not in allowed:
            raise StateTransitionError(
                f"Invalid refund_status transition from {record.refund_status.value} to {target.value}"
            )
        record.refund_status = target

    @staticmethod
    def to_response(record: DepositRecord) -> DepositResponse:
        return DepositResponse(
            deposit_id=record.deposit_id,
            challenge_id=record.challenge_id,
            crawler_id=record.crawler_id,
            site_id=record.site_id,
            policy_version=record.policy_version,
            required_amount=record.required_amount,
            captured_amount=record.captured_amount,
            currency=record.currency,
            payment_mode=record.payment_mode,
            simulated=(record.payment_mode.value == "mock"),
            deposit_state=record.deposit_state,
            payment_status=record.payment_status,
            policy_decision=record.policy_decision,
            refund_status=record.refund_status,
            policy_accepted=record.policy_accepted,
            policy_accepted_at=record.policy_accepted_at,
            session_id=record.session_id,
            provider_order_id=record.provider_order_id,
            provider_order_status=record.provider_order_status,
            approval_url=record.approval_url,
            provider_capture_id=record.provider_capture_id,
            provider_refund_id=record.provider_refund_id,
            provider_refund_status=record.provider_refund_status,
            failure_reason=record.failure_reason,
            decision_reason=record.decision_reason,
            created_at=record.created_at,
            updated_at=record.updated_at,
            captured_at=record.captured_at,
            refunded_at=record.refunded_at,
            forfeited_at=record.forfeited_at,
        )

    async def register_challenge(self, challenge: ChallengeRecord) -> None:
        """Persist a newly issued 402 challenge record."""
        async with self._lock:
            self._challenges[challenge.challenge_id] = challenge
            self._persist_unlocked()

    async def get_challenge(self, challenge_id: str) -> Optional[ChallengeRecord]:
        async with self._lock:
            rec = self._challenges.get(challenge_id)
            return rec.model_copy(deep=True) if rec else None

    async def get_deposit(self, deposit_id: str) -> Optional[DepositRecord]:
        async with self._lock:
            rec = self._deposits.get(deposit_id)
            return rec.model_copy(deep=True) if rec else None

    async def list_deposits(self) -> list[DepositRecord]:
        async with self._lock:
            return [rec.model_copy(deep=True) for rec in self._deposits.values()]

    @staticmethod
    def _refund_reconciliation_failure(
        lookup: RefundDetailsLookupResult,
        *,
        expected_refund_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> Optional[str]:
        if not lookup.refund_id:
            return "PayPal refund lookup omitted the refund ID; reconciliation remains pending."
        if lookup.refund_id != expected_refund_id:
            return "PayPal refund lookup returned a different refund ID; reconciliation remains pending."
        if lookup.status != "COMPLETED":
            status = lookup.status or "missing status"
            return f"PayPal refund status is {status}; reconciliation remains pending."
        if lookup.refunded_amount is None or not lookup.refunded_amount.is_finite():
            return "PayPal refund lookup omitted a valid amount; reconciliation remains pending."
        if lookup.refunded_amount != expected_amount:
            return "PayPal refund amount does not match the deposit; reconciliation remains pending."
        if not lookup.currency:
            return "PayPal refund lookup omitted the currency; reconciliation remains pending."
        if lookup.currency != expected_currency.upper():
            return "PayPal refund currency does not match the deposit; reconciliation remains pending."
        return None

    async def reconcile_refund(self, deposit_id: str) -> DepositRecord:
        """Reconcile an existing pending refund by its persisted provider refund ID."""
        async with self._lock:
            record = self._deposits.get(deposit_id)
            if record is None:
                raise KeyError(f"Unknown deposit_id: {deposit_id}")
            if record.refund_status == RefundTransactionStatus.REFUNDED:
                return record.model_copy(deep=True)
            if record.refund_status not in (
                RefundTransactionStatus.REFUND_PENDING,
                RefundTransactionStatus.REFUND_IN_PROGRESS,
            ):
                raise StateTransitionError(
                    "Only an existing pending refund can be reconciled."
                )
            if record.payment_status != PaymentStatus.CAPTURED:
                raise StateTransitionError(
                    "Cannot reconcile a refund unless the original payment is captured."
                )
            if record.policy_decision != PolicyDecisionStatus.ELIGIBLE_FOR_REFUND:
                raise StateTransitionError(
                    "Cannot reconcile a refund that is not eligible under the recorded policy decision."
                )
            if not record.provider_refund_id:
                raise StateTransitionError(
                    "Cannot reconcile a refund without its persisted PayPal refund ID."
                )
            if deposit_id in self._active_refunds:
                raise StateTransitionError(
                    "A refund submission is already in progress for this deposit."
                )
            if deposit_id in self._active_refund_reconciliations:
                raise StateTransitionError(
                    "Refund reconciliation is already in progress for this deposit."
                )

            if record.refund_status == RefundTransactionStatus.REFUND_IN_PROGRESS:
                self._transition_refund(record, RefundTransactionStatus.REFUND_PENDING)
                record.deposit_state = DepositState.REFUND_PENDING
                record.updated_at = utc_now_iso()
                self._persist_unlocked()
            self._active_refund_reconciliations.add(deposit_id)
            refund_id = record.provider_refund_id
            expected_amount = Decimal(
                record.captured_amount or record.required_amount
            ).quantize(Decimal("0.01"))
            expected_currency = record.currency.upper()

        try:
            try:
                lookup = await self._payment_provider.get_refund_details(
                    refund_id=refund_id
                )
                failure_reason = self._refund_reconciliation_failure(
                    lookup,
                    expected_refund_id=refund_id,
                    expected_amount=expected_amount,
                    expected_currency=expected_currency,
                )
            except PaymentProviderError as exc:
                lookup = None
                failure_reason = str(exc)

            async with self._lock:
                record = self._deposits[deposit_id]
                if record.refund_status == RefundTransactionStatus.REFUNDED:
                    return record.model_copy(deep=True)

                if lookup is not None and lookup.status:
                    record.provider_refund_status = lookup.status
                if failure_reason is None:
                    assert lookup is not None
                    self._transition_refund(record, RefundTransactionStatus.REFUNDED)
                    record.deposit_state = DepositState.REFUNDED
                    record.provider_refund_id = refund_id
                    record.provider_refund_status = lookup.status
                    record.refunded_at = utc_now_iso()
                    record.failure_reason = None
                    event_type = "DEPOSIT_REFUND_RECONCILED"
                    reason_code = "REFUND_RECONCILIATION_COMPLETED"
                    allowed = True
                else:
                    if record.refund_status == RefundTransactionStatus.REFUND_IN_PROGRESS:
                        self._transition_refund(
                            record, RefundTransactionStatus.REFUND_PENDING
                        )
                    record.deposit_state = DepositState.REFUND_PENDING
                    record.provider_refund_id = refund_id
                    record.failure_reason = failure_reason
                    record.updated_at = utc_now_iso()
                    event_type = "DEPOSIT_REFUND_RECONCILIATION_PENDING"
                    reason_code = "REFUND_RECONCILIATION_INCONCLUSIVE"
                    allowed = False

                record.updated_at = utc_now_iso()
                self._persist_unlocked()
                reconciled_copy = record.model_copy(deep=True)

            await self._audit.log_event(
                event_type=event_type,
                crawler_id=reconciled_copy.crawler_id,
                challenge_id=reconciled_copy.challenge_id,
                session_id=reconciled_copy.session_id,
                deposit_id=reconciled_copy.deposit_id,
                allowed=allowed,
                reason_code=reason_code,
                policy_version=reconciled_copy.policy_version,
                payment_mode=reconciled_copy.payment_mode.value,
                details={
                    "provider_refund_status": reconciled_copy.provider_refund_status,
                    "refund_status": reconciled_copy.refund_status.value,
                    "deposit_state": reconciled_copy.deposit_state.value,
                    "payment_status": reconciled_copy.payment_status.value,
                    "policy_decision": reconciled_copy.policy_decision.value,
                    "provider_refund_id_present": bool(reconciled_copy.provider_refund_id),
                    "failure_reason": reconciled_copy.failure_reason,
                    "simulated": self._payment_provider.mode.value == "mock",
                },
            )
            return reconciled_copy
        finally:
            async with self._lock:
                self._active_refund_reconciliations.discard(deposit_id)

    async def create_deposit(
        self,
        *,
        challenge_id: str,
        crawler_id: str,
    ) -> DepositRecord:
        """
        Validate the challenge, mark it consumed (single-use), create a pending
        deposit record with server-controlled deposit amount, and initiate the
        order via the configured payment provider.
        """
        valid_crawler = validate_crawler_id(crawler_id)

        async with self._lock:
            challenge = self._challenges.get(challenge_id)
            if challenge is None:
                raise KeyError(f"Unknown challenge_id: {challenge_id}")

            IdentityService.validate_challenge_for_deposit(challenge, valid_crawler)

            if challenge.consumed or challenge.deposit_id is not None:
                raise StateTransitionError(
                    f"Challenge {challenge_id} has already been consumed and cannot be reused"
                )

            deposit_id = f"dep_{uuid.uuid4().hex}"
            now_iso = utc_now_iso()
            record = DepositRecord(
                deposit_id=deposit_id,
                challenge_id=challenge_id,
                crawler_id=valid_crawler,
                site_id=challenge.site_id,
                policy_version=challenge.policy_version,
                required_amount=challenge.required_amount,
                currency=challenge.currency,
                payment_mode=self._payment_provider.mode,
                deposit_state=DepositState.PENDING,
                payment_status=PaymentStatus.PENDING,
                policy_decision=PolicyDecisionStatus.UNDECIDED,
                refund_status=RefundTransactionStatus.NOT_REQUESTED,
                order_request_id=f"ord_req_{deposit_id}",
                capture_request_id=f"cap_req_{deposit_id}",
                refund_request_id=f"ref_req_{deposit_id}",
                created_at=now_iso,
                updated_at=now_iso,
            )

            challenge.consumed = True
            challenge.deposit_id = deposit_id
            self._deposits[deposit_id] = record
            self._persist_unlocked()

        # Call provider outside the lock or re-acquire to update state
        required_decimal = Decimal(record.required_amount)
        try:
            order_res = await self._payment_provider.create_order(
                deposit_id=record.deposit_id,
                amount=required_decimal,
                currency=record.currency,
                request_id=record.order_request_id,
                description=(
                    f"CrawlerTrust refundable deposit for {record.site_id} "
                    f"(crawler={record.crawler_id})"
                ),
            )
        except PaymentProviderError as exc:
            async with self._lock:
                stored = self._deposits[record.deposit_id]
                self._transition_payment(stored, PaymentStatus.FAILED)
                stored.deposit_state = DepositState.FAILED
                stored.failure_reason = str(exc)
                stored.updated_at = utc_now_iso()
                self._persist_unlocked()
                failed_copy = stored.model_copy(deep=True)

            await self._audit.log_event(
                event_type="DEPOSIT_ORDER_FAILED",
                crawler_id=failed_copy.crawler_id,
                challenge_id=failed_copy.challenge_id,
                deposit_id=failed_copy.deposit_id,
                reason_code="PROVIDER_ORDER_CREATION_FAILED",
                policy_version=failed_copy.policy_version,
                payment_mode=failed_copy.payment_mode.value,
                details={"error": str(exc)},
            )
            raise

        async with self._lock:
            stored = self._deposits[record.deposit_id]
            stored.provider_order_id = order_res.order_id
            stored.provider_order_status = order_res.status
            stored.approval_url = order_res.approval_url
            if order_res.status == "APPROVED":
                self._transition_payment(stored, PaymentStatus.APPROVED)
                stored.deposit_state = DepositState.APPROVED
            else:
                stored.deposit_state = DepositState.PENDING
            stored.updated_at = utc_now_iso()
            self._persist_unlocked()
            updated_copy = stored.model_copy(deep=True)

        await self._audit.log_event(
            event_type="DEPOSIT_INITIATED",
            crawler_id=updated_copy.crawler_id,
            challenge_id=updated_copy.challenge_id,
            deposit_id=updated_copy.deposit_id,
            reason_code="ORDER_CREATED",
            policy_version=updated_copy.policy_version,
            payment_mode=updated_copy.payment_mode.value,
            details={
                "provider_order_id": updated_copy.provider_order_id,
                "provider_order_status": updated_copy.provider_order_status,
                "required_amount": updated_copy.required_amount,
                "currency": updated_copy.currency,
                "simulated": order_res.simulated,
            },
        )
        return updated_copy

    async def capture_deposit(
        self,
        *,
        deposit_id: str,
        crawler_id: str,
    ) -> DepositRecord:
        """
        Capture an order through the payment provider and validate captured
        amount, currency, capture ID, and completion status.
        """
        async with self._lock:
            record = self._deposits.get(deposit_id)
            if record is None:
                raise KeyError(f"Unknown deposit_id: {deposit_id}")
            if record.crawler_id != crawler_id:
                raise PermissionError("Deposit belongs to a different crawler_id")

            if record.payment_status == PaymentStatus.CAPTURED:
                return record.model_copy(deep=True)

            if not record.provider_order_id:
                raise StateTransitionError(
                    "Deposit does not have a valid provider_order_id to capture"
                )

            order_id = record.provider_order_id
            capture_req_id = record.capture_request_id
            expected_amt = Decimal(record.required_amount)
            expected_curr = record.currency

        try:
            verify_res = await self._payment_provider.capture_order(
                order_id=order_id,
                request_id=capture_req_id,
                expected_amount=expected_amt,
                expected_currency=expected_curr,
            )
        except PaymentApprovalRequiredError as exc:
            async with self._lock:
                stored = self._deposits[deposit_id]
                stored.failure_reason = str(exc)
                if exc.approval_url:
                    stored.approval_url = exc.approval_url
                stored.updated_at = utc_now_iso()
                self._persist_unlocked()
            await self._audit.log_event(
                event_type="PAYMENT_CAPTURE_PENDING_APPROVAL",
                crawler_id=crawler_id,
                deposit_id=deposit_id,
                reason_code="PAYER_APPROVAL_REQUIRED",
                payment_mode=self._payment_provider.mode.value,
                details={"message": str(exc), "approval_url": exc.approval_url},
            )
            raise
        except PaymentProviderError as exc:
            async with self._lock:
                stored = self._deposits[deposit_id]
                self._transition_payment(stored, PaymentStatus.FAILED)
                stored.deposit_state = DepositState.FAILED
                stored.failure_reason = str(exc)
                stored.updated_at = utc_now_iso()
                self._persist_unlocked()
            await self._audit.log_event(
                event_type="PAYMENT_CAPTURE_FAILED",
                crawler_id=crawler_id,
                deposit_id=deposit_id,
                reason_code="PROVIDER_CAPTURE_ERROR",
                payment_mode=self._payment_provider.mode.value,
                details={"error": str(exc)},
            )
            raise

        return await self._apply_verification_result(deposit_id, verify_res)

    async def verify_deposit(
        self,
        *,
        deposit_id: str,
        crawler_id: str,
    ) -> DepositRecord:
        """
        Perform authoritative server-side verification of the payment status,
        captured amount, currency, and capture ID with the payment provider.
        """
        async with self._lock:
            record = self._deposits.get(deposit_id)
            if record is None:
                raise KeyError(f"Unknown deposit_id: {deposit_id}")
            if record.crawler_id != crawler_id:
                raise PermissionError("Deposit belongs to a different crawler_id")
            if not record.provider_order_id:
                raise StateTransitionError("Deposit has no provider_order_id to verify")

            order_id = record.provider_order_id
            expected_amt = Decimal(record.required_amount)
            expected_curr = record.currency

        try:
            verify_res = await self._payment_provider.verify_order(
                order_id=order_id,
                expected_amount=expected_amt,
                expected_currency=expected_curr,
            )
        except PaymentProviderError as exc:
            await self._audit.log_event(
                event_type="PAYMENT_VERIFICATION_ERROR",
                crawler_id=crawler_id,
                deposit_id=deposit_id,
                reason_code="PROVIDER_VERIFY_ERROR",
                payment_mode=self._payment_provider.mode.value,
                details={"error": str(exc)},
            )
            raise

        return await self._apply_verification_result(deposit_id, verify_res)

    async def _apply_verification_result(
        self,
        deposit_id: str,
        verify_res: object,
    ) -> DepositRecord:
        from server.services.payment_service import OrderVerifyResult

        assert isinstance(verify_res, OrderVerifyResult)

        async with self._lock:
            stored = self._deposits[deposit_id]
            stored.provider_order_status = verify_res.order_status
            if verify_res.approval_url:
                stored.approval_url = verify_res.approval_url

            if verify_res.verified and verify_res.capture_id and verify_res.captured_amount is not None:
                if stored.payment_status != PaymentStatus.CAPTURED:
                    self._transition_payment(stored, PaymentStatus.CAPTURED)
                if stored.deposit_state in (DepositState.PENDING, DepositState.APPROVED, DepositState.FAILED):
                    stored.deposit_state = DepositState.CAPTURED
                stored.provider_capture_id = verify_res.capture_id
                stored.captured_amount = f"{verify_res.captured_amount:.2f}"
                stored.failure_reason = None
                if not stored.captured_at:
                    stored.captured_at = utc_now_iso()
                stored.updated_at = utc_now_iso()
                self._persist_unlocked()
                verified_copy = stored.model_copy(deep=True)
                success = True
            else:
                # If amount or currency mismatched on a captured order, mark FAILED permanently
                if verify_res.capture_id and verify_res.failure_reason:
                    self._transition_payment(stored, PaymentStatus.FAILED)
                    stored.deposit_state = DepositState.FAILED
                elif verify_res.order_status == "APPROVED" and stored.payment_status == PaymentStatus.PENDING:
                    self._transition_payment(stored, PaymentStatus.APPROVED)
                    stored.deposit_state = DepositState.APPROVED
                stored.failure_reason = verify_res.failure_reason or "Payment not verified"
                stored.updated_at = utc_now_iso()
                self._persist_unlocked()
                verified_copy = stored.model_copy(deep=True)
                success = False

        if success:
            await self._audit.log_event(
                event_type="PAYMENT_VERIFIED",
                crawler_id=verified_copy.crawler_id,
                challenge_id=verified_copy.challenge_id,
                deposit_id=verified_copy.deposit_id,
                allowed=True,
                reason_code="CAPTURE_VERIFIED",
                policy_version=verified_copy.policy_version,
                payment_mode=verified_copy.payment_mode.value,
                details={
                    "provider_order_id": verified_copy.provider_order_id,
                    "provider_capture_id": verified_copy.provider_capture_id,
                    "captured_amount": verified_copy.captured_amount,
                    "currency": verified_copy.currency,
                    "simulated": verify_res.simulated,
                },
            )
        else:
            await self._audit.log_event(
                event_type="PAYMENT_VERIFICATION_REJECTED",
                crawler_id=verified_copy.crawler_id,
                challenge_id=verified_copy.challenge_id,
                deposit_id=verified_copy.deposit_id,
                allowed=False,
                reason_code="VERIFICATION_FAILED",
                policy_version=verified_copy.policy_version,
                payment_mode=verified_copy.payment_mode.value,
                details={
                    "provider_order_id": verified_copy.provider_order_id,
                    "order_status": verify_res.order_status,
                    "failure_reason": verified_copy.failure_reason,
                },
            )

        return verified_copy

    async def record_policy_acceptance_and_bind_session(
        self,
        *,
        deposit_id: str,
        challenge_id: str,
        crawler_id: str,
        policy_version: str,
        agree_to_terms: bool,
        session_id: str,
        accepted_at: str,
    ) -> DepositRecord:
        """
        Validate that the deposit is CAPTURED, matches challenge_id, crawler_id, and
        policy_version, has not already issued a session, and bind it to session_id.
        """
        if not agree_to_terms:
            raise ValueError("Crawler must explicitly set agree_to_terms=true to accept policy")

        if policy_version != self._site_config.policy_version:
            raise ValueError(
                f"Policy version mismatch: expected '{self._site_config.policy_version}', "
                f"received '{policy_version}'"
            )

        async with self._lock:
            record = self._deposits.get(deposit_id)
            if record is None:
                raise KeyError(f"Unknown deposit_id: {deposit_id}")

            if record.crawler_id != crawler_id:
                raise PermissionError("Deposit belongs to a different crawler_id")

            if record.challenge_id != challenge_id:
                raise ValueError(
                    "challenge_id does not match the challenge bound to this deposit"
                )

            if record.payment_status != PaymentStatus.CAPTURED or not record.provider_capture_id:
                raise StateTransitionError(
                    "Deposit payment must be CAPTURED and verified before accepting policy"
                )

            if record.policy_accepted or record.session_id is not None:
                raise StateTransitionError(
                    "Policy has already been accepted and a session was already issued for this deposit"
                )

            record.policy_accepted = True
            record.policy_accepted_at = accepted_at
            record.session_id = session_id
            record.updated_at = utc_now_iso()
            self._persist_unlocked()
            return record.model_copy(deep=True)

    async def evaluate_and_process_refund(
        self,
        *,
        deposit_id: str,
        crawler_id: str,
        session: Optional[SessionRecord],
    ) -> tuple[DepositRecord, bool, str]:
        """
        Evaluate refund eligibility from authoritative session and deposit history.
        - Separates `payment_status`, `policy_decision`, and `refund_status`.
        - Preserves `payment_status == CAPTURED` even if a refund fails or a policy is violated.
        - Idempotent on repeated calls.
        """
        async with self._lock:
            record = self._deposits.get(deposit_id)
            if record is None:
                raise KeyError(f"Unknown deposit_id: {deposit_id}")

            if record.crawler_id != crawler_id:
                raise PermissionError("Deposit belongs to a different crawler_id")

            # Idempotent return if already REFUNDED
            if record.refund_status == RefundTransactionStatus.REFUNDED:
                reason = record.decision_reason or "Deposit has already been refunded."
                return record.model_copy(deep=True), True, reason

            # Idempotent return if already FORFEITED by policy
            if (
                record.policy_decision == PolicyDecisionStatus.FORFEITED_BY_POLICY
                and record.deposit_state == DepositState.FORFEITED
            ):
                reason = record.decision_reason or "Deposit was forfeited due to policy violations."
                return record.model_copy(deep=True), False, reason

            # Verify payment was actually captured
            if (
                record.payment_status != PaymentStatus.CAPTURED
                or not record.provider_capture_id
                or not record.captured_amount
            ):
                raise StateTransitionError(
                    "Cannot evaluate refund for a deposit whose payment is not CAPTURED"
                )

            # Verify policy was accepted and session exists
            if not record.policy_accepted or session is None or record.session_id != session.session_id:
                raise StateTransitionError(
                    "Cannot evaluate refund before policy acceptance and session creation"
                )

            # Check if session has disqualifying violations
            if session.disqualified:
                reasons_joined = "; ".join(session.disqualification_reasons) or (
                    "Disqualifying crawling policy violation recorded"
                )
                self._transition_decision(
                    record, PolicyDecisionStatus.FORFEITED_BY_POLICY
                )
                self._transition_refund(
                    record, RefundTransactionStatus.NOT_APPLICABLE_FORFEITED
                )
                record.deposit_state = DepositState.FORFEITED
                record.decision_reason = (
                    f"Deposit forfeited under site policy (retained in merchant account; "
                    f"no refund issued): {reasons_joined}"
                )
                record.forfeited_at = utc_now_iso()
                record.updated_at = utc_now_iso()
                self._persist_unlocked()
                forfeited_copy = record.model_copy(deep=True)
                decision_reason = record.decision_reason
                is_forfeited = True
            else:
                is_forfeited = False
                forfeited_copy = None
                decision_reason = ""

        if is_forfeited and forfeited_copy is not None:
            await self._audit.log_event(
                event_type="DEPOSIT_FORFEITED",
                crawler_id=forfeited_copy.crawler_id,
                challenge_id=forfeited_copy.challenge_id,
                session_id=session.session_id if session else None,
                deposit_id=forfeited_copy.deposit_id,
                allowed=False,
                reason_code="FORFEITED_BY_POLICY",
                policy_version=forfeited_copy.policy_version,
                payment_mode=forfeited_copy.payment_mode.value,
                details={
                    "decision_reason": decision_reason,
                    "disqualification_reasons": session.disqualification_reasons if session else [],
                    "total_violations": session.total_violations if session else 0,
                    "payment_status": forfeited_copy.payment_status.value,
                    "policy_decision": forfeited_copy.policy_decision.value,
                    "refund_status": forfeited_copy.refund_status.value,
                },
            )
            return forfeited_copy, False, decision_reason

        assert session is not None

        # Require session completion before issuing a refund
        if (
            self._site_config.refund_policy.require_explicit_session_completion
            and session.status != SessionStatus.COMPLETED
        ):
            raise StateTransitionError(
                f"Session must be in COMPLETED status before refund (current status: {session.status.value})"
            )

        async with self._lock:
            current_record = self._deposits[deposit_id]
            has_existing_refund_id = bool(current_record.provider_refund_id) and (
                current_record.refund_status
                in (
                    RefundTransactionStatus.REFUND_PENDING,
                    RefundTransactionStatus.REFUND_IN_PROGRESS,
                )
            )
        if has_existing_refund_id:
            reconciled = await self.reconcile_refund(deposit_id)
            return (
                reconciled,
                True,
                reconciled.decision_reason or "Existing refund was reconciled.",
            )

        # Mark refund in progress inside the lock to prevent concurrent duplicate provider calls
        async with self._lock:
            record = self._deposits[deposit_id]
            if record.refund_status == RefundTransactionStatus.REFUNDED:
                return (
                    record.model_copy(deep=True),
                    True,
                    record.decision_reason or "Already refunded",
                )
            if (
                record.refund_status == RefundTransactionStatus.REFUND_IN_PROGRESS
                and deposit_id in self._active_refunds
            ):
                raise StateTransitionError(
                    "A refund request is already in progress for this deposit"
                )

            reconcile_before_refund = record.refund_status in (
                RefundTransactionStatus.REFUND_IN_PROGRESS,
                RefundTransactionStatus.REFUND_PENDING,
            )
            self._transition_decision(record, PolicyDecisionStatus.ELIGIBLE_FOR_REFUND)
            if record.refund_status != RefundTransactionStatus.REFUND_IN_PROGRESS:
                self._transition_refund(record, RefundTransactionStatus.REFUND_IN_PROGRESS)
            record.deposit_state = DepositState.REFUND_PENDING
            record.decision_reason = (
                "Session completed with 0 disqualifying violations; eligible for full deposit refund."
            )
            record.updated_at = utc_now_iso()
            self._persist_unlocked()
            self._active_refunds.add(deposit_id)

            capture_id = record.provider_capture_id or ""
            order_id = record.provider_order_id
            refund_amt = Decimal(record.captured_amount or record.required_amount)
            currency = record.currency
            refund_req_id = record.refund_request_id

        # Execute provider refund outside lock using deterministic PayPal-Request-Id
        try:
            refund_result = await self._payment_provider.refund_capture(
                capture_id=capture_id,
                order_id=order_id,
                amount=refund_amt,
                currency=currency,
                request_id=refund_req_id,
                reason=f"CrawlerTrust compliant session refund ({session.session_id})",
                reconcile_before_refund=reconcile_before_refund,
            )
        except (Exception, asyncio.CancelledError):
            async with self._lock:
                self._active_refunds.discard(deposit_id)
                record = self._deposits[deposit_id]
                if record.refund_status == RefundTransactionStatus.REFUND_IN_PROGRESS:
                    self._transition_refund(record, RefundTransactionStatus.REFUND_PENDING)
                record.deposit_state = DepositState.REFUND_PENDING
                record.provider_refund_status = "UNKNOWN"
                record.failure_reason = (
                    "Refund outcome is unknown; reconcile with PayPal before another attempt."
                )
                record.updated_at = utc_now_iso()
                self._persist_unlocked()
            await self._audit.log_event(
                event_type="DEPOSIT_REFUND_PENDING",
                crawler_id=record.crawler_id,
                challenge_id=record.challenge_id,
                session_id=session.session_id,
                deposit_id=record.deposit_id,
                allowed=False,
                reason_code="REFUND_OUTCOME_UNKNOWN",
                policy_version=record.policy_version,
                payment_mode=record.payment_mode.value,
                details={
                    "provider_capture_id": record.provider_capture_id,
                    "provider_refund_status": record.provider_refund_status,
                    "payment_status": record.payment_status.value,
                    "policy_decision": record.policy_decision.value,
                    "refund_status": record.refund_status.value,
                },
            )
            raise

        if refund_result.status == "COMPLETED" and (
            not refund_result.refund_id
            or refund_result.refunded_amount is None
            or refund_result.refunded_amount.quantize(Decimal("0.01"))
            != refund_amt.quantize(Decimal("0.01"))
            or not refund_result.currency
            or refund_result.currency.upper() != currency.upper()
        ):
            refund_result = RefundExecutionResult(
                refund_id=refund_result.refund_id,
                status="PENDING",
                refunded_amount=refund_result.refunded_amount,
                currency=refund_result.currency,
                simulated=refund_result.simulated,
                failure_reason=(
                    "Refund provider result did not include a matching ID, amount, and currency; "
                    "manual reconciliation is required."
                ),
                provider_status=refund_result.provider_status or "COMPLETED",
            )

        async with self._lock:
            self._active_refunds.discard(deposit_id)
            record = self._deposits[deposit_id]
            record.provider_refund_id = refund_result.refund_id
            record.provider_refund_status = refund_result.provider_status or refund_result.status

            if refund_result.status == "COMPLETED":
                self._transition_refund(record, RefundTransactionStatus.REFUNDED)
                record.deposit_state = DepositState.REFUNDED
                record.failure_reason = None
                record.refunded_at = utc_now_iso()
                audit_event_type = "DEPOSIT_REFUNDED"
                audit_reason = "REFUND_COMPLETED"
            elif refund_result.status == "PENDING":
                self._transition_refund(record, RefundTransactionStatus.REFUND_PENDING)
                record.deposit_state = DepositState.REFUND_PENDING
                record.failure_reason = refund_result.failure_reason
                audit_event_type = "DEPOSIT_REFUND_PENDING"
                audit_reason = "REFUND_PENDING_UPSTREAM"
            else:
                # Preserve captured-payment record intact when refund fails!
                self._transition_refund(record, RefundTransactionStatus.REFUND_FAILED)
                record.deposit_state = DepositState.CAPTURED
                record.failure_reason = (
                    refund_result.failure_reason or "Upstream refund execution failed"
                )
                audit_event_type = "DEPOSIT_REFUND_FAILED"
                audit_reason = "REFUND_EXECUTION_FAILED"

            record.updated_at = utc_now_iso()
            self._persist_unlocked()
            final_copy = record.model_copy(deep=True)

        await self._audit.log_event(
            event_type=audit_event_type,
            crawler_id=final_copy.crawler_id,
            challenge_id=final_copy.challenge_id,
            session_id=session.session_id,
            deposit_id=final_copy.deposit_id,
            allowed=(refund_result.status == "COMPLETED"),
            reason_code=audit_reason,
            policy_version=final_copy.policy_version,
            payment_mode=final_copy.payment_mode.value,
            details={
                "provider_capture_id": final_copy.provider_capture_id,
                "provider_refund_id": final_copy.provider_refund_id,
                "provider_refund_status": final_copy.provider_refund_status,
                "payment_status": final_copy.payment_status.value,
                "policy_decision": final_copy.policy_decision.value,
                "refund_status": final_copy.refund_status.value,
                "deposit_state": final_copy.deposit_state.value,
                "failure_reason": final_copy.failure_reason,
                "simulated": refund_result.simulated,
            },
        )
        if (
            refund_result.status == "PENDING"
            and final_copy.provider_refund_id
        ):
            reconciled = await self.reconcile_refund(deposit_id)
            return reconciled, True, final_copy.decision_reason or ""

        return final_copy, True, final_copy.decision_reason or ""
