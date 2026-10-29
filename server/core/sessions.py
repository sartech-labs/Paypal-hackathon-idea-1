"""Session registry, token authentication, and atomic session persistence."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from server.core.security import (
     PersistenceCorruptionError,
    StateTransitionError,
    atomic_write_json,
    constant_time_equals,
    generate_bearer_token,
    hash_bearer_token,
    is_timestamp_expired,
    safe_load_json,
    utc_future_iso,
    utc_now_iso,
)
from server.schemas import (
    ALLOWED_SESSION_TRANSITIONS,
    PaymentMode,
    RefundPolicyConfig,
    SessionIssueResponse,
    SessionRecord,
    SessionStatus,
    SessionStatusResponse,
    SiteConfig,
    ViolationEntry,
)


class SessionManager:
    """Manages crawler sessions, bearer token hashes, and atomic disk snapshots."""

    def __init__(self, sessions_db_path: Path, site_config: SiteConfig) -> None:
        self._db_path = sessions_db_path
        self._site_config = site_config
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionRecord] = {}
        self._deposit_to_session: dict[str, str] = {}

    def load_from_disk(self) -> None:
        """Load persisted sessions on startup and expire any stale ACTIVE sessions."""
        raw = safe_load_json(self._db_path, expected_top_keys=("sessions",))
        loaded_sessions: dict[str, SessionRecord] = {}
        deposit_map: dict[str, str] = {}
        dirty = False

        for session_id, session_data in raw["sessions"].items():
            try:
                record = SessionRecord.model_validate(session_data)
            except ValidationError as exc:
                raise PersistenceCorruptionError(
                    f"Invalid SessionRecord for {session_id} in {self._db_path}: {exc}"
                ) from exc

            if record.session_id != session_id:
                raise PersistenceCorruptionError(
                    f"Mismatched session_id key {session_id} != {record.session_id}"
                )

            if record.status == SessionStatus.ACTIVE and is_timestamp_expired(
                record.expires_at
            ):
                record.status = SessionStatus.EXPIRED
                dirty = True

            loaded_sessions[session_id] = record
            deposit_map[record.deposit_id] = session_id

        self._sessions = loaded_sessions
        self._deposit_to_session = deposit_map
        if dirty:
            self._persist_unlocked()

    def _persist_unlocked(self) -> None:
        payload = {
            "sessions": {
                sid: record.model_dump(mode="json")
                for sid, record in self._sessions.items()
            }
        }
        atomic_write_json(self._db_path, payload)

    @staticmethod
    def _transition_status(record: SessionRecord, target: SessionStatus) -> None:
        allowed = ALLOWED_SESSION_TRANSITIONS.get(record.status, set())
        if target not in allowed:
            raise StateTransitionError(
                f"Invalid session state transition from {record.status.value} to {target.value}"
            )
        record.status = target

    async def issue_session(
        self,
        *,
        crawler_id: str,
        challenge_id: str,
        deposit_id: str,
        policy_version: str,
        policy_accepted_at: str,
        payment_reference: str,
        payment_mode: PaymentMode,
        session_ttl_seconds: int,
    ) -> tuple[SessionRecord, SessionIssueResponse]:
        """Issue a new session bound to a verified deposit and accepted policy."""
        async with self._lock:
            if deposit_id in self._deposit_to_session:
                raise StateTransitionError(
                    f"Deposit {deposit_id} is already bound to session {self._deposit_to_session[deposit_id]}"
                )

            raw_token = generate_bearer_token()
            token_hash = hash_bearer_token(raw_token)
            session_id = f"ses_{uuid.uuid4().hex}"
            now_iso = utc_now_iso()
            expires_iso = utc_future_iso(session_ttl_seconds)

            record = SessionRecord(
                session_id=session_id,
                crawler_id=crawler_id,
                challenge_id=challenge_id,
                deposit_id=deposit_id,
                policy_version=policy_version,
                policy_accepted_at=policy_accepted_at,
                payment_reference=payment_reference,
                payment_mode=payment_mode,
                token_hash=token_hash,
                status=SessionStatus.ACTIVE,
                created_at=now_iso,
                expires_at=expires_iso,
            )

            self._sessions[session_id] = record
            self._deposit_to_session[deposit_id] = session_id
            self._persist_unlocked()

            issue_response = SessionIssueResponse(
                session_id=session_id,
                crawler_id=crawler_id,
                deposit_id=deposit_id,
                policy_version=policy_version,
                policy_accepted_at=policy_accepted_at,
                payment_mode=payment_mode,
                simulated_payment=(payment_mode == PaymentMode.MOCK),
                access_token=raw_token,
                status=record.status,
                created_at=now_iso,
                expires_at=expires_iso,
                max_requests_per_session=self._site_config.max_requests_per_session,
                rate_limit_requests=self._site_config.rate_limit_requests,
                rate_limit_window_seconds=self._site_config.rate_limit_window_seconds,
            )
            return record, issue_response

    async def authenticate_token(self, raw_token: str) -> Optional[SessionRecord]:
        """
        Look up a session by constant-time comparison of the SHA-256 token hash.
        Automatically updates status to EXPIRED if the session TTL has elapsed.
        """
        if not raw_token or not raw_token.strip():
            return None

        candidate_hash = hash_bearer_token(raw_token.strip())
        async with self._lock:
            matched: Optional[SessionRecord] = None
            for record in self._sessions.values():
                if constant_time_equals(record.token_hash, candidate_hash):
                    matched = record
                    break

            if matched is None:
                return None

            if matched.status == SessionStatus.ACTIVE and is_timestamp_expired(
                matched.expires_at
            ):
                self._transition_status(matched, SessionStatus.EXPIRED)
                self._persist_unlocked()

            return matched.model_copy(deep=True)

    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """Retrieve a session by ID, refreshing expiration state if needed."""
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            if record.status == SessionStatus.ACTIVE and is_timestamp_expired(
                record.expires_at
            ):
                self._transition_status(record, SessionStatus.EXPIRED)
                self._persist_unlocked()
            return record.model_copy(deep=True)

    async def get_session_by_deposit(self, deposit_id: str) -> Optional[SessionRecord]:
        """Retrieve the session bound to deposit_id, if any."""
        async with self._lock:
            session_id = self._deposit_to_session.get(deposit_id)
            if not session_id:
                return None
            record = self._sessions.get(session_id)
            if record is None:
                return None
            if record.status == SessionStatus.ACTIVE and is_timestamp_expired(
                record.expires_at
            ):
                self._transition_status(record, SessionStatus.EXPIRED)
                self._persist_unlocked()
            return record.model_copy(deep=True)

    async def record_gateway_evaluation(
        self,
        *,
        session_id: str,
        path: str,
        method: str,
        allowed: bool,
        reason_code: str,
        reason: str,
        violation: bool,
        disqualifying_immediate: bool,
        refund_policy: RefundPolicyConfig,
    ) -> SessionRecord:
        """
        Record an authenticated gateway request against the session and update
        violation counters and disqualification status atomically.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                raise KeyError(f"Unknown session_id: {session_id}")

            record.total_requests += 1
            if allowed:
                record.allowed_requests += 1
            else:
                record.rejected_requests += 1

            is_disqualifying = False
            if violation:
                record.total_violations += 1
                if reason_code in ("DISALLOWED_PATH", "INVALID_PATH"):
                    record.disallowed_path_violations += 1
                    if refund_policy.disqualify_on_disallowed_path or disqualifying_immediate:
                        is_disqualifying = True
                        msg = f"Disallowed/invalid path access on {path} ({reason_code})"
                        if msg not in record.disqualification_reasons:
                            record.disqualification_reasons.append(msg)
                elif reason_code == "RATE_LIMIT_EXCEEDED":
                    record.rate_limit_violations += 1
                    if (
                        record.rate_limit_violations
                        >= refund_policy.max_rate_limit_violations_before_disqualification
                    ):
                        is_disqualifying = True
                        msg = (
                            f"Rate limit violations ({record.rate_limit_violations}) reached "
                            f"disqualification threshold ({refund_policy.max_rate_limit_violations_before_disqualification})"
                        )
                        if msg not in record.disqualification_reasons:
                            record.disqualification_reasons.append(msg)
                elif reason_code == "MAX_REQUESTS_EXCEEDED":
                    record.budget_violations += 1
                    if (
                        record.budget_violations
                        >= refund_policy.max_budget_violations_before_disqualification
                    ):
                        is_disqualifying = True
                        msg = (
                            f"Session budget violations ({record.budget_violations}) reached "
                            f"disqualification threshold ({refund_policy.max_budget_violations_before_disqualification})"
                        )
                        if msg not in record.disqualification_reasons:
                            record.disqualification_reasons.append(msg)
                elif disqualifying_immediate:
                    is_disqualifying = True
                    msg = f"Policy violation ({reason_code}): {reason}"
                    if msg not in record.disqualification_reasons:
                        record.disqualification_reasons.append(msg)

                if is_disqualifying:
                    record.disqualified = True

                record.violations.append(
                    ViolationEntry(
                        timestamp=utc_now_iso(),
                        path=path,
                        method=method,
                        reason_code=reason_code,
                        reason=reason,
                        disqualifying=is_disqualifying,
                    )
                )

            self._persist_unlocked()
            return record.model_copy(deep=True)

    async def complete_session(self, session_id: str) -> SessionRecord:
        """
        Mark an ACTIVE session as COMPLETED (or return idempotently if already COMPLETED).
        Rejects REVOKED or EXPIRED sessions.
        """
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                raise KeyError(f"Unknown session_id: {session_id}")

            if record.status == SessionStatus.ACTIVE and is_timestamp_expired(
                record.expires_at
            ):
                self._transition_status(record, SessionStatus.EXPIRED)
                self._persist_unlocked()

            if record.status == SessionStatus.COMPLETED:
                return record.model_copy(deep=True)

            if record.status != SessionStatus.ACTIVE:
                raise StateTransitionError(
                    f"Cannot complete session in status {record.status.value}"
                )

            self._transition_status(record, SessionStatus.COMPLETED)
            record.completed_at = utc_now_iso()
            self._persist_unlocked()
            return record.model_copy(deep=True)

    async def revoke_session(self, session_id: str) -> SessionRecord:
        """Revoke a session, preventing any further protected access or completion."""
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                raise KeyError(f"Unknown session_id: {session_id}")

            if record.status == SessionStatus.REVOKED:
                return record.model_copy(deep=True)

            if record.status != SessionStatus.ACTIVE:
                raise StateTransitionError(
                    f"Cannot revoke session in status {record.status.value}"
                )

            self._transition_status(record, SessionStatus.REVOKED)
            record.revoked_at = utc_now_iso()
            self._persist_unlocked()
            return record.model_copy(deep=True)

    async def list_sessions(self) -> list[SessionRecord]:
        """Return copies of all session records."""
        async with self._lock:
            return [rec.model_copy(deep=True) for rec in self._sessions.values()]

    @staticmethod
    def to_status_response(record: SessionRecord) -> SessionStatusResponse:
        """Convert an internal SessionRecord (excluding token_hash) to a public schema."""
        return SessionStatusResponse(
            session_id=record.session_id,
            crawler_id=record.crawler_id,
            challenge_id=record.challenge_id,
            deposit_id=record.deposit_id,
            policy_version=record.policy_version,
            policy_accepted_at=record.policy_accepted_at,
            payment_mode=record.payment_mode,
            status=record.status,
            created_at=record.created_at,
            expires_at=record.expires_at,
            completed_at=record.completed_at,
            revoked_at=record.revoked_at,
            total_requests=record.total_requests,
            allowed_requests=record.allowed_requests,
            rejected_requests=record.rejected_requests,
            total_violations=record.total_violations,
            disallowed_path_violations=record.disallowed_path_violations,
            rate_limit_violations=record.rate_limit_violations,
            budget_violations=record.budget_violations,
            disqualified=record.disqualified,
            disqualification_reasons=list(record.disqualification_reasons),
            violations=list(record.violations),
        )

