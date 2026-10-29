"""Payment provider interfaces and implementations (Mock and PayPal Sandbox v2)."""

from __future__ import annotations

import abc
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from server.config import Settings
from server.schemas import PaymentMode


class PaymentProviderError(RuntimeError):
    """Raised when a payment provider request fails or returns an invalid payload."""


class PaymentProviderTimeoutError(PaymentProviderError):
    """Raised when an upstream payment provider call times out."""


class PaymentApprovalRequiredError(PaymentProviderError):
    """Raised when a PayPal Sandbox order requires payer browser approval before capture."""

    def __init__(self, message: str, approval_url: Optional[str] = None) -> None:
        super().__init__(message)
        self.approval_url = approval_url


@dataclass(frozen=True)
class OrderCreationResult:
    order_id: str
    status: str
    approval_url: Optional[str]
    simulated: bool
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderVerifyResult:
    order_id: str
    order_status: str
    capture_id: Optional[str]
    capture_status: Optional[str]
    captured_amount: Optional[Decimal]
    currency: Optional[str]
    verified: bool
    simulated: bool
    approval_url: Optional[str] = None
    failure_reason: Optional[str] = None


@dataclass(frozen=True)
class RefundExecutionResult:
    refund_id: Optional[str]
    status: str  # COMPLETED | PENDING | FAILED
    refunded_amount: Optional[Decimal]
    currency: Optional[str]
    simulated: bool
    failure_reason: Optional[str] = None


class PaymentProvider(abc.ABC):
    """Abstract interface for deposit order creation, capture, verification, and refunds."""

    @property
    @abc.abstractmethod
    def mode(self) -> PaymentMode:
        """Return the active PaymentMode."""

    @abc.abstractmethod
    async def create_order(
        self,
        *,
        deposit_id: str,
        amount: Decimal,
        currency: str,
        request_id: str,
        description: str,
    ) -> OrderCreationResult:
        """Create a deposit order."""

    @abc.abstractmethod
    async def capture_order(
        self,
        *,
        order_id: str,
        request_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        """Capture an approved order and verify captured amount and currency."""

    @abc.abstractmethod
    async def verify_order(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        """Query the provider to authoritatively verify order capture status, amount, and currency."""

    @abc.abstractmethod
    async def refund_capture(
        self,
        *,
        capture_id: str,
        order_id: Optional[str],
        amount: Decimal,
        currency: str,
        request_id: str,
        reason: str,
    ) -> RefundExecutionResult:
        """Refund a previously captured payment idempotently."""


class MockPaymentProvider(PaymentProvider):
    """
    Deterministic mock payment provider for local development and automated testing.
    Clearly labels all records as simulated (`payment_mode="mock"`).
    """

    def __init__(self) -> None:
        self._orders: dict[str, dict[str, Any]] = {}
        self._order_by_request_id: dict[str, str] = {}
        self._refund_by_request_id: dict[str, RefundExecutionResult] = {}
        self._refund_by_capture_id: dict[str, RefundExecutionResult] = {}

    @property
    def mode(self) -> PaymentMode:
        return PaymentMode.MOCK

    async def create_order(
        self,
        *,
        deposit_id: str,
        amount: Decimal,
        currency: str,
        request_id: str,
        description: str,
    ) -> OrderCreationResult:
        if request_id in self._order_by_request_id:
            existing_id = self._order_by_request_id[request_id]
            existing = self._orders[existing_id]
            return OrderCreationResult(
                order_id=existing_id,
                status=str(existing["status"]),
                approval_url=existing["approval_url"],
                simulated=True,
                raw_metadata={"simulated": True, "deposit_id": deposit_id},
            )

        order_id = f"mock_order_{secrets.token_hex(8)}"
        approval_url = f"https://sandbox.paypal.example.invalid/checkoutnow?token={order_id}&simulated=true"
        self._orders[order_id] = {
            "order_id": order_id,
            "deposit_id": deposit_id,
            "amount": amount.quantize(Decimal("0.01")),
            "currency": currency.upper(),
            "status": "APPROVED",
            "approval_url": approval_url,
            "capture_id": None,
            "capture_status": None,
            "description": description,
        }
        self._order_by_request_id[request_id] = order_id
        return OrderCreationResult(
            order_id=order_id,
            status="APPROVED",
            approval_url=approval_url,
            simulated=True,
            raw_metadata={"simulated": True, "deposit_id": deposit_id},
        )

    async def capture_order(
        self,
        *,
        order_id: str,
        request_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        order = self._orders.get(order_id)
        if order is None:
            raise PaymentProviderError(f"Mock order not found: {order_id}")

        if order["capture_id"] is None:
            order["capture_id"] = f"mock_cap_{secrets.token_hex(8)}"
            order["capture_status"] = "COMPLETED"
            order["status"] = "COMPLETED"

        return await self.verify_order(
            order_id=order_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
        )

    async def verify_order(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        order = self._orders.get(order_id)
        if order is None:
            return OrderVerifyResult(
                order_id=order_id,
                order_status="NOT_FOUND",
                capture_id=None,
                capture_status=None,
                captured_amount=None,
                currency=None,
                verified=False,
                simulated=True,
                failure_reason=f"Order {order_id} does not exist in mock provider.",
            )

        captured_amt: Decimal = order["amount"]
        curr: str = order["currency"]
        cap_id: Optional[str] = order["capture_id"]
        cap_status: Optional[str] = order["capture_status"]
        ord_status: str = order["status"]

        if ord_status != "COMPLETED" or cap_status != "COMPLETED" or not cap_id:
            return OrderVerifyResult(
                order_id=order_id,
                order_status=ord_status,
                capture_id=cap_id,
                capture_status=cap_status,
                captured_amount=captured_amt,
                currency=curr,
                verified=False,
                simulated=True,
                approval_url=order.get("approval_url"),
                failure_reason="Order has not been captured yet.",
            )

        if captured_amt != expected_amount.quantize(Decimal("0.01")):
            return OrderVerifyResult(
                order_id=order_id,
                order_status=ord_status,
                capture_id=cap_id,
                capture_status=cap_status,
                captured_amount=captured_amt,
                currency=curr,
                verified=False,
                simulated=True,
                failure_reason=(
                    f"Captured amount {captured_amt:.2f} does not match required deposit {expected_amount:.2f}"
                ),
            )

        if curr.upper() != expected_currency.upper():
            return OrderVerifyResult(
                order_id=order_id,
                order_status=ord_status,
                capture_id=cap_id,
                capture_status=cap_status,
                captured_amount=captured_amt,
                currency=curr,
                verified=False,
                simulated=True,
                failure_reason=(
                    f"Captured currency {curr} does not match required currency {expected_currency}"
                ),
            )

        return OrderVerifyResult(
            order_id=order_id,
            order_status=ord_status,
            capture_id=cap_id,
            capture_status=cap_status,
            captured_amount=captured_amt,
            currency=curr,
            verified=True,
            simulated=True,
        )

    async def refund_capture(
        self,
        *,
        capture_id: str,
        order_id: Optional[str],
        amount: Decimal,
        currency: str,
        request_id: str,
        reason: str,
    ) -> RefundExecutionResult:
        if request_id in self._refund_by_request_id:
            return self._refund_by_request_id[request_id]
        if capture_id in self._refund_by_capture_id:
            return self._refund_by_capture_id[capture_id]

        refund_id = f"mock_ref_{secrets.token_hex(8)}"
        result = RefundExecutionResult(
            refund_id=refund_id,
            status="COMPLETED",
            refunded_amount=amount.quantize(Decimal("0.01")),
            currency=currency.upper(),
            simulated=True,
        )
        self._refund_by_request_id[request_id] = result
        self._refund_by_capture_id[capture_id] = result
        return result


class PayPalSandboxProvider(PaymentProvider):
    """
    PayPal Sandbox API v2 payment provider implementing:
    - OAuth 2.0 client_credentials token exchange (`/v1/oauth2/token`)
    - Orders API v2 order creation (`/v2/checkout/orders`)
    - Orders API v2 order capture (`/v2/checkout/orders/{id}/capture`)
    - Orders API v2 authoritative order lookup (`/v2/checkout/orders/{id}`)
    - Payments API v2 capture refund (`/v2/payments/captures/{capture_id}/refund`)
    """

    def __init__(
        self,
        settings: Settings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = settings.paypal_base_url.rstrip("/")
        self._client_id = settings.paypal_client_id.strip()
        self._client_secret = settings.paypal_client_secret.strip()
        self._timeout = httpx.Timeout(settings.paypal_timeout_seconds)
        self._transport = transport
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def mode(self) -> PaymentMode:
        return PaymentMode.PAYPAL_SANDBOX

    def _ensure_credentials(self) -> None:
        if not self._client_id or not self._client_secret:
            raise PaymentProviderError(
                "PayPal Sandbox credentials (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET) "
                "are not configured."
            )

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
        )

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        self._ensure_credentials()
        now = time.monotonic()
        if self._cached_token and now < self._token_expires_at:
            return self._cached_token

        try:
            response = await client.post(
                "/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise PaymentProviderTimeoutError(
                "Timed out obtaining OAuth 2.0 token from PayPal Sandbox"
            ) from exc
        except httpx.HTTPError as exc:
            raise PaymentProviderError(
                f"HTTP error communicating with PayPal OAuth endpoint: {exc}"
            ) from exc

        if response.status_code != 200:
            raise PaymentProviderError(
                f"PayPal OAuth token request failed with HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentProviderError("Invalid JSON in PayPal OAuth response") from exc

        token = payload.get("access_token")
        if not token or not isinstance(token, str):
            raise PaymentProviderError("PayPal OAuth response missing access_token")

        expires_in = int(payload.get("expires_in", 3600))
        self._cached_token = token
        self._token_expires_at = now + max(60, expires_in - 60)
        return token

    @staticmethod
    def _extract_approval_url(payload: dict[str, Any]) -> Optional[str]:
        links = payload.get("links", [])
        if not isinstance(links, list):
            return None
        for link in links:
            if isinstance(link, dict) and link.get("rel") in ("approve", "payer-action"):
                href = link.get("href")
                if isinstance(href, str) and href:
                    return href
        return None

    @classmethod
    def _parse_order_verification(
        cls,
        payload: dict[str, Any],
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        order_id = str(payload.get("id", ""))
        order_status = str(payload.get("status", "UNKNOWN"))
        approval_url = cls._extract_approval_url(payload)

        purchase_units = payload.get("purchase_units", [])
        capture_obj: Optional[dict[str, Any]] = None
        if isinstance(purchase_units, list) and purchase_units:
            first_unit = purchase_units[0]
            if isinstance(first_unit, dict):
                payments = first_unit.get("payments", {})
                if isinstance(payments, dict):
                    captures = payments.get("captures", [])
                    if isinstance(captures, list) and captures and isinstance(captures[0], dict):
                        capture_obj = captures[0]

        if capture_obj is None:
            return OrderVerifyResult(
                order_id=order_id,
                order_status=order_status,
                capture_id=None,
                capture_status=None,
                captured_amount=None,
                currency=None,
                verified=False,
                simulated=False,
                approval_url=approval_url,
                failure_reason=(
                    f"PayPal order {order_id} is in status '{order_status}' with no completed capture."
                ),
            )

        capture_id = str(capture_obj.get("id", "")).strip() or None
        capture_status = str(capture_obj.get("status", "")).strip() or None
        amount_obj = capture_obj.get("amount", {})
        raw_value = amount_obj.get("value") if isinstance(amount_obj, dict) else None
        raw_curr = amount_obj.get("currency_code") if isinstance(amount_obj, dict) else None

        parsed_amount: Optional[Decimal] = None
        if raw_value is not None:
            try:
                parsed_amount = Decimal(str(raw_value)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                parsed_amount = None

        currency_code = str(raw_curr).strip().upper() if raw_curr else None

        if order_status != "COMPLETED" or capture_status != "COMPLETED" or not capture_id:
            return OrderVerifyResult(
                order_id=order_id,
                order_status=order_status,
                capture_id=capture_id,
                capture_status=capture_status,
                captured_amount=parsed_amount,
                currency=currency_code,
                verified=False,
                simulated=False,
                approval_url=approval_url,
                failure_reason=(
                    f"Order status='{order_status}', capture status='{capture_status}' (expected COMPLETED)."
                ),
            )

        expected_quantized = expected_amount.quantize(Decimal("0.01"))
        if parsed_amount is None or parsed_amount != expected_quantized:
            return OrderVerifyResult(
                order_id=order_id,
                order_status=order_status,
                capture_id=capture_id,
                capture_status=capture_status,
                captured_amount=parsed_amount,
                currency=currency_code,
                verified=False,
                simulated=False,
                failure_reason=(
                    f"Captured amount '{parsed_amount}' does not match required deposit '{expected_quantized}'."
                ),
            )

        if currency_code != expected_currency.upper():
            return OrderVerifyResult(
                order_id=order_id,
                order_status=order_status,
                capture_id=capture_id,
                capture_status=capture_status,
                captured_amount=parsed_amount,
                currency=currency_code,
                verified=False,
                simulated=False,
                failure_reason=(
                    f"Captured currency '{currency_code}' does not match required currency '{expected_currency.upper()}'."
                ),
            )

        return OrderVerifyResult(
            order_id=order_id,
            order_status=order_status,
            capture_id=capture_id,
            capture_status=capture_status,
            captured_amount=parsed_amount,
            currency=currency_code,
            verified=True,
            simulated=False,
            approval_url=approval_url,
        )

    async def create_order(
        self,
        *,
        deposit_id: str,
        amount: Decimal,
        currency: str,
        request_id: str,
        description: str,
    ) -> OrderCreationResult:
        quantized = amount.quantize(Decimal("0.01"))
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            body = {
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "custom_id": deposit_id,
                        "description": description[:127],
                        "amount": {
                            "currency_code": currency.upper(),
                            "value": f"{quantized:.2f}",
                        },
                    }
                ],
            }
            try:
                response = await client.post(
                    "/v2/checkout/orders",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": request_id,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise PaymentProviderTimeoutError(
                    "Timed out creating order in PayPal Sandbox"
                ) from exc
            except httpx.HTTPError as exc:
                raise PaymentProviderError(
                    f"HTTP error creating PayPal order: {exc}"
                ) from exc

            if response.status_code not in (200, 201):
                raise PaymentProviderError(
                    f"PayPal create order failed with HTTP {response.status_code}"
                )

            payload = response.json()
            order_id = str(payload.get("id", "")).strip()
            if not order_id:
                raise PaymentProviderError("PayPal create order response missing order id")
            status = str(payload.get("status", "CREATED"))
            approval_url = self._extract_approval_url(payload)
            return OrderCreationResult(
                order_id=order_id,
                status=status,
                approval_url=approval_url,
                simulated=False,
                raw_metadata={"id": order_id, "status": status},
            )

    async def capture_order(
        self,
        *,
        order_id: str,
        request_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            try:
                response = await client.post(
                    f"/v2/checkout/orders/{order_id}/capture",
                    json={},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": request_id,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise PaymentProviderTimeoutError(
                    f"Timed out capturing PayPal order {order_id}"
                ) from exc
            except httpx.HTTPError as exc:
                raise PaymentProviderError(
                    f"HTTP error capturing PayPal order {order_id}: {exc}"
                ) from exc

            if response.status_code in (200, 201):
                payload = response.json()
                return self._parse_order_verification(
                    payload, expected_amount, expected_currency
                )

            # Check if order was already captured or requires payer approval
            error_details = ""
            try:
                err_json = response.json()
                issue_list = err_json.get("details", [])
                issues = [
                    str(item.get("issue", ""))
                    for item in issue_list
                    if isinstance(item, dict)
                ]
                error_name = str(err_json.get("name", ""))
                error_details = f"{error_name}: {','.join(issues)}"
                if "ORDER_ALREADY_CAPTURED" in issues:
                    return await self.verify_order(
                        order_id=order_id,
                        expected_amount=expected_amount,
                        expected_currency=expected_currency,
                    )
                if "ORDER_NOT_APPROVED" in issues or "PAYER_ACTION_REQUIRED" in issues:
                    # Fetch current order to get approval_url
                    current = await self.verify_order(
                        order_id=order_id,
                        expected_amount=expected_amount,
                        expected_currency=expected_currency,
                    )
                    raise PaymentApprovalRequiredError(
                        f"PayPal order {order_id} has not been approved by the payer yet.",
                        approval_url=current.approval_url,
                    )
            except PaymentApprovalRequiredError:
                raise
            except Exception:
                pass

            raise PaymentProviderError(
                f"PayPal order capture failed with HTTP {response.status_code} ({error_details})"
            )

    async def verify_order(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> OrderVerifyResult:
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            try:
                response = await client.get(
                    f"/v2/checkout/orders/{order_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise PaymentProviderTimeoutError(
                    f"Timed out verifying PayPal order {order_id}"
                ) from exc
            except httpx.HTTPError as exc:
                raise PaymentProviderError(
                    f"HTTP error verifying PayPal order {order_id}: {exc}"
                ) from exc

            if response.status_code != 200:
                raise PaymentProviderError(
                    f"PayPal order lookup failed with HTTP {response.status_code}"
                )

            payload = response.json()
            return self._parse_order_verification(
                payload, expected_amount, expected_currency
            )

    async def _reconcile_existing_refund(
        self,
        client: httpx.AsyncClient,
        token: str,
        order_id: Optional[str],
        capture_id: str,
    ) -> Optional[RefundExecutionResult]:
        """
        Inspect the order in PayPal to see if a refund already exists for capture_id
        before issuing or retrying a refund request.
        """
        if not order_id:
            return None
        try:
            response = await client.get(
                f"/v2/checkout/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            units = payload.get("purchase_units", [])
            if not isinstance(units, list) or not units:
                return None
            payments = units[0].get("payments", {}) if isinstance(units[0], dict) else {}
            refunds = payments.get("refunds", []) if isinstance(payments, dict) else []
            if not isinstance(refunds, list) or not refunds:
                return None
            first_refund = refunds[0]
            if not isinstance(first_refund, dict):
                return None
            ref_id = str(first_refund.get("id", "")).strip() or None
            ref_status = str(first_refund.get("status", "UNKNOWN")).strip().upper()
            amt_obj = first_refund.get("amount", {})
            val = amt_obj.get("value") if isinstance(amt_obj, dict) else None
            curr = amt_obj.get("currency_code") if isinstance(amt_obj, dict) else None
            parsed_amt = Decimal(str(val)).quantize(Decimal("0.01")) if val else None
            return RefundExecutionResult(
                refund_id=ref_id,
                status=ref_status,
                refunded_amount=parsed_amt,
                currency=str(curr).upper() if curr else None,
                simulated=False,
            )
        except Exception:
            return None

    async def refund_capture(
        self,
        *,
        capture_id: str,
        order_id: Optional[str],
        amount: Decimal,
        currency: str,
        request_id: str,
        reason: str,
    ) -> RefundExecutionResult:
        quantized = amount.quantize(Decimal("0.01"))
        async with self._build_client() as client:
            token = await self._get_access_token(client)

            # Reconcile first in case a previous attempt succeeded before a crash/timeout
            existing = await self._reconcile_existing_refund(
                client, token, order_id, capture_id
            )
            if existing is not None and existing.status in ("COMPLETED", "PENDING"):
                return existing

            body = {
                "amount": {
                    "value": f"{quantized:.2f}",
                    "currency_code": currency.upper(),
                },
                "note_to_payer": reason[:255],
            }
            try:
                response = await client.post(
                    f"/v2/payments/captures/{capture_id}/refund",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": request_id,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except httpx.TimeoutException:
                return RefundExecutionResult(
                    refund_id=None,
                    status="FAILED",
                    refunded_amount=None,
                    currency=currency.upper(),
                    simulated=False,
                    failure_reason="Timed out waiting for PayPal refund response",
                )
            except httpx.HTTPError as exc:
                return RefundExecutionResult(
                    refund_id=None,
                    status="FAILED",
                    refunded_amount=None,
                    currency=currency.upper(),
                    simulated=False,
                    failure_reason=f"HTTP error calling PayPal refund endpoint: {exc}",
                )

            if response.status_code not in (200, 201):
                return RefundExecutionResult(
                    refund_id=None,
                    status="FAILED",
                    refunded_amount=None,
                    currency=currency.upper(),
                    simulated=False,
                    failure_reason=f"PayPal refund returned HTTP {response.status_code}",
                )

            payload = response.json()
            refund_id = str(payload.get("id", "")).strip() or None
            raw_status = str(payload.get("status", "FAILED")).strip().upper()
            amt_obj = payload.get("amount", {})
            raw_val = amt_obj.get("value") if isinstance(amt_obj, dict) else None
            raw_curr = amt_obj.get("currency_code") if isinstance(amt_obj, dict) else currency

            parsed_amt = quantized
            if raw_val is not None:
                try:
                    parsed_amt = Decimal(str(raw_val)).quantize(Decimal("0.01"))
                except (InvalidOperation, ValueError):
                    parsed_amt = quantized

            if raw_status == "COMPLETED":
                return RefundExecutionResult(
                    refund_id=refund_id,
                    status="COMPLETED",
                    refunded_amount=parsed_amt,
                    currency=str(raw_curr).upper(),
                    simulated=False,
                )
            if raw_status == "PENDING":
                return RefundExecutionResult(
                    refund_id=refund_id,
                    status="PENDING",
                    refunded_amount=parsed_amt,
                    currency=str(raw_curr).upper(),
                    simulated=False,
                    failure_reason="PayPal refund is currently PENDING settlement.",
                )
            return RefundExecutionResult(
                refund_id=refund_id,
                status="FAILED",
                refunded_amount=None,
                currency=str(raw_curr).upper(),
                simulated=False,
                failure_reason=f"PayPal refund status was '{raw_status}'.",
            )

