"""Payment provider interfaces and implementations (Mock and PayPal Sandbox v2)."""

from __future__ import annotations

import abc
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import quote, urlencode

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
class CapturedPaymentLookupResult:
    capture_id: str
    status: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class RefundExecutionResult:
    refund_id: Optional[str]
    status: str  # COMPLETED | PENDING | FAILED
    refunded_amount: Optional[Decimal]
    currency: Optional[str]
    simulated: bool
    failure_reason: Optional[str] = None
    provider_status: Optional[str] = None


@dataclass(frozen=True)
class RefundDetailsLookupResult:
    refund_id: Optional[str]
    status: Optional[str]
    refunded_amount: Optional[Decimal]
    currency: Optional[str]


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of authorizing (placing a hold on) a deposit order."""

    order_id: str
    order_status: str
    authorization_id: Optional[str]
    authorization_status: Optional[str]
    authorized_amount: Optional[Decimal]
    currency: Optional[str]
    verified: bool
    simulated: bool
    approval_url: Optional[str] = None
    failure_reason: Optional[str] = None


@dataclass(frozen=True)
class SettlementResult:
    """Outcome of settling an authorization: VOIDED, CAPTURED, PENDING, or FAILED."""

    status: str
    simulated: bool
    capture_id: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    provider_status: Optional[str] = None
    failure_reason: Optional[str] = None


def _parse_money(amount_obj: Any) -> tuple[Optional[Decimal], Optional[str]]:
    """Parse a PayPal money object into (amount, currency), tolerating malformed input."""
    if not isinstance(amount_obj, dict):
        return None, None
    amount: Optional[Decimal] = None
    raw_value = amount_obj.get("value")
    if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool):
        try:
            parsed = Decimal(str(raw_value))
            if parsed.is_finite():
                amount = parsed.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            amount = None
    raw_currency = amount_obj.get("currency_code")
    currency = raw_currency.strip().upper() if isinstance(raw_currency, str) and raw_currency.strip() else None
    return amount, currency


def _evaluate_authorization(
    *,
    order_id: str,
    order_status: str,
    authorization_id: Optional[str],
    authorization_status: Optional[str],
    amount: Optional[Decimal],
    currency: Optional[str],
    expected_amount: Decimal,
    expected_currency: str,
    simulated: bool,
    approval_url: Optional[str] = None,
) -> AuthorizationResult:
    """Accept an authorization only when it exists, is CREATED, and matches the deposit."""

    def result(verified: bool, failure_reason: Optional[str] = None) -> AuthorizationResult:
        return AuthorizationResult(
            order_id=order_id,
            order_status=order_status,
            authorization_id=authorization_id,
            authorization_status=authorization_status,
            authorized_amount=amount,
            currency=currency,
            verified=verified,
            simulated=simulated,
            approval_url=approval_url,
            failure_reason=failure_reason,
        )

    if not authorization_id or not authorization_status:
        return result(False, f"Order {order_id} is in status '{order_status}' with no authorization yet.")
    if authorization_status != "CREATED":
        return result(False, f"Authorization status is '{authorization_status}' (expected CREATED).")
    expected = expected_amount.quantize(Decimal("0.01"))
    if amount is None or amount != expected:
        return result(False, f"Authorized amount '{amount}' does not match required deposit '{expected}'.")
    if currency != expected_currency.upper():
        return result(
            False,
            f"Authorized currency '{currency}' does not match required currency '{expected_currency.upper()}'.",
        )
    return result(True)


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
        intent: str = "CAPTURE",
    ) -> OrderCreationResult:
        """Create a deposit order with intent CAPTURE or AUTHORIZE."""

    @abc.abstractmethod
    async def authorize_order(
        self,
        *,
        order_id: str,
        request_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        """Authorize an approved AUTHORIZE-intent order, placing a hold on the deposit."""

    @abc.abstractmethod
    async def verify_authorization(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        """Look up the order's authorization and verify status, amount, and currency."""

    @abc.abstractmethod
    async def void_authorization(
        self,
        *,
        authorization_id: str,
        order_id: Optional[str],
        request_id: str,
    ) -> SettlementResult:
        """Release a hold without capturing funds (compliant session)."""

    @abc.abstractmethod
    async def capture_authorization(
        self,
        *,
        authorization_id: str,
        order_id: Optional[str],
        amount: Decimal,
        currency: str,
        request_id: str,
        note: str,
    ) -> SettlementResult:
        """Capture a held deposit (forfeiture)."""

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
        reconcile_before_refund: bool = False,
    ) -> RefundExecutionResult:
        """Refund a previously captured payment idempotently."""

    @abc.abstractmethod
    async def get_refund_details(self, *, refund_id: str) -> RefundDetailsLookupResult:
        """Retrieve an existing refund without submitting a new financial operation."""


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
        intent: str = "CAPTURE",
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
            "intent": intent.upper(),
            "status": "APPROVED",
            "approval_url": approval_url,
            "capture_id": None,
            "capture_status": None,
            "authorization_id": None,
            "authorization_status": None,
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
        reconcile_before_refund: bool = False,
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

    async def get_refund_details(self, *, refund_id: str) -> RefundDetailsLookupResult:
        for result in self._refund_by_capture_id.values():
            if result.refund_id == refund_id:
                return RefundDetailsLookupResult(
                    refund_id=result.refund_id,
                    status=result.provider_status or result.status,
                    refunded_amount=result.refunded_amount,
                    currency=result.currency,
                )
        raise PaymentProviderError("Mock refund was not found.")

    def _order_for_authorization(self, authorization_id: str) -> Optional[dict[str, Any]]:
        for order in self._orders.values():
            if order.get("authorization_id") == authorization_id:
                return order
        return None

    async def authorize_order(
        self,
        *,
        order_id: str,
        request_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        order = self._orders.get(order_id)
        if order is None:
            raise PaymentProviderError(f"Mock order not found: {order_id}")
        if order["intent"] != "AUTHORIZE":
            raise PaymentProviderError(f"Mock order {order_id} was not created with intent AUTHORIZE.")
        if order["authorization_id"] is None:
            order["authorization_id"] = f"mock_auth_{secrets.token_hex(8)}"
            order["authorization_status"] = "CREATED"
            order["status"] = "COMPLETED"
        return await self.verify_authorization(
            order_id=order_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
        )

    async def verify_authorization(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        order = self._orders.get(order_id)
        if order is None:
            return AuthorizationResult(
                order_id=order_id,
                order_status="NOT_FOUND",
                authorization_id=None,
                authorization_status=None,
                authorized_amount=None,
                currency=None,
                verified=False,
                simulated=True,
                failure_reason=f"Order {order_id} does not exist in mock provider.",
            )
        return _evaluate_authorization(
            order_id=order_id,
            order_status=str(order["status"]),
            authorization_id=order["authorization_id"],
            authorization_status=order["authorization_status"],
            amount=order["amount"],
            currency=order["currency"],
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            simulated=True,
            approval_url=order["approval_url"],
        )

    async def void_authorization(
        self,
        *,
        authorization_id: str,
        order_id: Optional[str],
        request_id: str,
    ) -> SettlementResult:
        order = self._order_for_authorization(authorization_id)
        if order is None:
            return SettlementResult(status="FAILED", simulated=True, failure_reason="Mock authorization not found.")
        if order["authorization_status"] == "CAPTURED":
            return SettlementResult(
                status="FAILED",
                simulated=True,
                provider_status="CAPTURED",
                failure_reason="Authorization was already captured and cannot be voided.",
            )
        order["authorization_status"] = "VOIDED"
        return SettlementResult(status="VOIDED", simulated=True, provider_status="VOIDED")

    async def capture_authorization(
        self,
        *,
        authorization_id: str,
        order_id: Optional[str],
        amount: Decimal,
        currency: str,
        request_id: str,
        note: str,
    ) -> SettlementResult:
        order = self._order_for_authorization(authorization_id)
        if order is None:
            return SettlementResult(status="FAILED", simulated=True, failure_reason="Mock authorization not found.")
        if order["authorization_status"] == "VOIDED":
            return SettlementResult(
                status="FAILED",
                simulated=True,
                provider_status="VOIDED",
                failure_reason="Authorization was already voided and cannot be captured.",
            )
        if order["capture_id"] is None:
            order["capture_id"] = f"mock_cap_{secrets.token_hex(8)}"
            order["capture_status"] = "COMPLETED"
            order["authorization_status"] = "CAPTURED"
        return SettlementResult(
            status="CAPTURED",
            simulated=True,
            capture_id=order["capture_id"],
            amount=amount.quantize(Decimal("0.01")),
            currency=currency.upper(),
            provider_status="COMPLETED",
        )


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
        self._app_public_url = settings.app_public_url.rstrip("/")
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
        intent: str = "CAPTURE",
    ) -> OrderCreationResult:
        quantized = amount.quantize(Decimal("0.01"))
        if intent.upper() not in ("CAPTURE", "AUTHORIZE"):
            raise PaymentProviderError(f"Unsupported PayPal order intent: {intent!r}")
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            callback_query = urlencode({"deposit_id": deposit_id})
            body = {
                "intent": intent.upper(),
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
                "payment_source": {
                    "paypal": {
                        "experience_context": {
                            "return_url": (
                                f"{self._app_public_url}/deposits/paypal/return?"
                                f"{callback_query}"
                            ),
                            "cancel_url": (
                                f"{self._app_public_url}/deposits/paypal/cancel?"
                                f"{callback_query}"
                            ),
                        }
                    }
                },
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

    @staticmethod
    def _parse_refund_response(
        payload: Any,
        *,
        expected_amount: Decimal,
        expected_currency: str,
        source: str,
    ) -> RefundExecutionResult:
        if not isinstance(payload, dict):
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=f"{source} returned a malformed refund response.",
            )

        raw_id = payload.get("id")
        refund_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
        raw_status = payload.get("status")
        provider_status = (
            raw_status.strip().upper()
            if isinstance(raw_status, str) and raw_status.strip()
            else None
        )
        amount_obj = payload.get("amount")
        raw_value = amount_obj.get("value") if isinstance(amount_obj, dict) else None
        raw_currency = amount_obj.get("currency_code") if isinstance(amount_obj, dict) else None

        refunded_amount: Optional[Decimal] = None
        if isinstance(raw_value, (str, int, float)) and not isinstance(raw_value, bool):
            try:
                parsed_amount = Decimal(str(raw_value))
                if parsed_amount.is_finite():
                    refunded_amount = parsed_amount.quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                pass
        currency = (
            raw_currency.strip().upper()
            if isinstance(raw_currency, str) and raw_currency.strip()
            else None
        )

        if provider_status == "COMPLETED":
            if not refund_id:
                reason = "PayPal reported COMPLETED without a refund ID."
            elif refunded_amount is None:
                reason = "PayPal reported COMPLETED without a valid refund amount."
            elif currency is None:
                reason = "PayPal reported COMPLETED without a refund currency."
            elif refunded_amount != expected_amount.quantize(Decimal("0.01")):
                reason = "PayPal's completed refund amount does not match the requested amount."
            elif currency != expected_currency.upper():
                reason = "PayPal's completed refund currency does not match the requested currency."
            else:
                return RefundExecutionResult(
                    refund_id=refund_id,
                    status="COMPLETED",
                    refunded_amount=refunded_amount,
                    currency=currency,
                    simulated=False,
                    provider_status=provider_status,
                )
            return RefundExecutionResult(
                refund_id=refund_id,
                status="PENDING",
                refunded_amount=refunded_amount,
                currency=currency,
                simulated=False,
                failure_reason=f"{reason} Manual reconciliation is required.",
                provider_status=provider_status,
            )

        if provider_status == "PENDING":
            return RefundExecutionResult(
                refund_id=refund_id,
                status="PENDING",
                refunded_amount=refunded_amount,
                currency=currency,
                simulated=False,
                failure_reason="PayPal refund is currently PENDING settlement.",
                provider_status=provider_status,
            )

        if provider_status in ("FAILED", "CANCELLED"):
            return RefundExecutionResult(
                refund_id=refund_id,
                status="FAILED",
                refunded_amount=refunded_amount,
                currency=currency,
                simulated=False,
                failure_reason=f"PayPal refund status was '{provider_status}'.",
                provider_status=provider_status,
            )

        return RefundExecutionResult(
            refund_id=refund_id,
            status="PENDING",
            refunded_amount=refunded_amount,
            currency=currency,
            simulated=False,
            failure_reason=(
                f"{source} did not provide a recognized refund status; "
                "manual reconciliation is required."
            ),
            provider_status=provider_status,
        )

    async def _get_captured_payment(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        capture_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> CapturedPaymentLookupResult:
        try:
            response = await client.get(
                f"/v2/payments/captures/{capture_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise PaymentProviderTimeoutError(
                "Timed out retrieving the PayPal capture for refund reconciliation."
            ) from exc
        except httpx.HTTPError as exc:
            raise PaymentProviderError(
                "Transport error retrieving the PayPal capture for refund reconciliation "
                f"({type(exc).__name__})."
            ) from exc

        if response.status_code != 200:
            raise PaymentProviderError(
                "PayPal capture lookup failed with HTTP "
                f"{response.status_code}; refund reconciliation is inconclusive."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentProviderError(
                "PayPal capture lookup returned unreadable JSON; "
                "refund reconciliation is inconclusive."
            ) from exc
        if not isinstance(payload, dict):
            raise PaymentProviderError(
                "PayPal capture lookup returned a malformed response; "
                "refund reconciliation is inconclusive."
            )

        returned_id = payload.get("id")
        if not isinstance(returned_id, str) or not returned_id.strip():
            raise PaymentProviderError(
                "PayPal capture lookup omitted the capture ID; "
                "refund reconciliation is inconclusive."
            )
        if returned_id.strip() != capture_id:
            raise PaymentProviderError(
                "PayPal capture lookup returned a different capture ID; "
                "refund reconciliation is inconclusive."
            )

        raw_status = payload.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            raise PaymentProviderError(
                "PayPal capture lookup omitted the capture status; "
                "refund reconciliation is inconclusive."
            )

        amount_obj = payload.get("amount")
        if not isinstance(amount_obj, dict):
            raise PaymentProviderError(
                "PayPal capture lookup omitted the amount; "
                "refund reconciliation is inconclusive."
            )
        raw_value = amount_obj.get("value")
        if not isinstance(raw_value, (str, int, float)) or isinstance(raw_value, bool):
            raise PaymentProviderError(
                "PayPal capture lookup omitted a valid amount; "
                "refund reconciliation is inconclusive."
            )
        try:
            amount = Decimal(str(raw_value))
            if not amount.is_finite():
                raise InvalidOperation
            amount = amount.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise PaymentProviderError(
                "PayPal capture lookup returned an invalid amount; "
                "refund reconciliation is inconclusive."
            ) from None
        if amount != expected_amount.quantize(Decimal("0.01")):
            raise PaymentProviderError(
                "PayPal capture amount does not match the persisted payment; "
                "refund reconciliation is inconclusive."
            )

        raw_currency = amount_obj.get("currency_code")
        if not isinstance(raw_currency, str) or not raw_currency.strip():
            raise PaymentProviderError(
                "PayPal capture lookup omitted the currency; "
                "refund reconciliation is inconclusive."
            )
        currency = raw_currency.strip().upper()
        if currency != expected_currency.upper():
            raise PaymentProviderError(
                "PayPal capture currency does not match the persisted payment; "
                "refund reconciliation is inconclusive."
            )

        return CapturedPaymentLookupResult(
            capture_id=returned_id.strip(),
            status=raw_status.strip().upper(),
            amount=amount,
            currency=currency,
        )

    async def get_captured_payment(
        self,
        *,
        capture_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> CapturedPaymentLookupResult:
        """Retrieve and validate one capture using the Payments v2 API."""
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            return await self._get_captured_payment(
                client,
                token,
                capture_id=capture_id,
                expected_amount=expected_amount,
                expected_currency=expected_currency,
            )

    @staticmethod
    def _refund_details_from_payload(payload: Any) -> RefundDetailsLookupResult:
        if not isinstance(payload, dict):
            raise PaymentProviderError(
                "PayPal refund lookup returned a malformed response."
            )

        raw_id = payload.get("id")
        refund_id = raw_id if isinstance(raw_id, str) and raw_id else None
        raw_status = payload.get("status")
        provider_status = (
            raw_status.strip().upper()
            if isinstance(raw_status, str) and raw_status.strip()
            else None
        )

        amount_obj = payload.get("amount")
        raw_amount = amount_obj.get("value") if isinstance(amount_obj, dict) else None
        refunded_amount: Optional[Decimal] = None
        if isinstance(raw_amount, (str, int, float)) and not isinstance(raw_amount, bool):
            try:
                parsed_amount = Decimal(str(raw_amount))
                if parsed_amount.is_finite():
                    refunded_amount = parsed_amount
            except (InvalidOperation, ValueError):
                pass

        raw_currency = amount_obj.get("currency_code") if isinstance(amount_obj, dict) else None
        currency = (
            raw_currency.strip().upper()
            if isinstance(raw_currency, str) and raw_currency.strip()
            else None
        )
        return RefundDetailsLookupResult(
            refund_id=refund_id,
            status=provider_status,
            refunded_amount=refunded_amount,
            currency=currency,
        )

    @staticmethod
    def _safe_refund_lookup_error_metadata(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""

        metadata: list[str] = []
        for field in ("name", "debug_id"):
            value = payload.get(field)
            if isinstance(value, str):
                safe_value = "".join(
                    character for character in value[:80]
                    if character.isalnum() or character in "._-"
                )
                if safe_value:
                    metadata.append(f"{field}={safe_value}")
        return f" ({', '.join(metadata)})" if metadata else ""

    async def _get_refund_details(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        refund_id: str,
    ) -> RefundDetailsLookupResult:
        if not refund_id.strip():
            raise PaymentProviderError("Cannot look up a PayPal refund without its persisted refund ID.")

        try:
            response = await client.get(
                f"/v2/payments/refunds/{quote(refund_id, safe='')}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise PaymentProviderTimeoutError(
                "Timed out retrieving PayPal refund details; reconciliation remains pending."
            ) from exc
        except httpx.HTTPError as exc:
            raise PaymentProviderError(
                "Transport error retrieving PayPal refund details "
                f"({type(exc).__name__}); reconciliation remains pending."
            ) from exc

        if response.status_code != 200:
            raise PaymentProviderError(
                "PayPal refund lookup failed with HTTP "
                f"{response.status_code}{self._safe_refund_lookup_error_metadata(response)}; "
                "reconciliation remains pending."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentProviderError(
                "PayPal refund lookup returned unreadable JSON; reconciliation remains pending."
            ) from exc
        return self._refund_details_from_payload(payload)

    async def get_refund_details(self, *, refund_id: str) -> RefundDetailsLookupResult:
        """Retrieve an existing PayPal refund through the Payments v2 API."""
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            return await self._get_refund_details(
                client,
                token,
                refund_id=refund_id,
            )

    async def _reconcile_existing_refund(
        self,
        client: httpx.AsyncClient,
        token: str,
        order_id: Optional[str],
        capture_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> RefundExecutionResult:
        """
        Reconcile details only after capture status indicates a refund may exist.
        An absent or empty refund collection is never proof that no refund exists.
        """
        if not order_id:
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason="Cannot reconcile refund without its PayPal order ID.",
            )
        try:
            response = await client.get(
                f"/v2/checkout/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if response.status_code != 200:
                return RefundExecutionResult(
                    refund_id=None,
                    status="PENDING",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "Could not reconcile the existing refund with PayPal "
                        f"(order lookup returned HTTP {response.status_code})."
                    ),
                )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=(
                    "Could not read PayPal's refund reconciliation response "
                    f"({type(exc).__name__})."
                ),
            )

        units = payload.get("purchase_units") if isinstance(payload, dict) else None
        if not isinstance(units, list):
            units = []

        matching_units: list[dict[str, Any]] = []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            payments = unit.get("payments")
            if not isinstance(payments, dict):
                continue
            captures = payments.get("captures")
            if isinstance(captures, list) and any(
                isinstance(capture, dict) and capture.get("id") == capture_id
                for capture in captures
            ):
                matching_units.append(unit)

        if len(matching_units) != 1:
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=(
                    "PayPal's order lookup did not unambiguously confirm the captured payment; "
                    "no additional refund was submitted."
                ),
            )

        payments = matching_units[0]["payments"]
        if "refunds" not in payments:
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=(
                    "PayPal did not include refund information in the order lookup; "
                    "no additional refund was submitted."
                ),
            )
        refunds = payments.get("refunds", [])
        if not isinstance(refunds, list):
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=(
                    "PayPal returned an invalid refunds collection; "
                    "no additional refund was submitted."
                ),
            )
        if not refunds:
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=(
                    "Capture status indicates a refund, but the order lookup did not provide "
                    "refund details; manual reconciliation is required."
                ),
                provider_status=None,
            )
        if len(refunds) != 1 or not isinstance(refunds[0], dict):
            return RefundExecutionResult(
                refund_id=None,
                status="PENDING",
                refunded_amount=None,
                currency=None,
                simulated=False,
                failure_reason=(
                    "PayPal reports multiple or malformed refunds for this payment; "
                    "manual reconciliation is required."
                ),
            )

        return self._parse_refund_response(
            refunds[0],
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            source="PayPal refund reconciliation",
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
        reconcile_before_refund: bool = False,
    ) -> RefundExecutionResult:
        quantized = amount.quantize(Decimal("0.01"))
        async with self._build_client() as client:
            try:
                token = await self._get_access_token(client)
            except (PaymentProviderError, ValueError, TypeError, AttributeError) as exc:
                return RefundExecutionResult(
                    refund_id=None,
                    status="FAILED",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "PayPal credentials or token acquisition failed before the refund request "
                        f"was submitted ({type(exc).__name__})."
                    ),
                )

            try:
                capture = await self._get_captured_payment(
                    client,
                    token,
                    capture_id=capture_id,
                    expected_amount=quantized,
                    expected_currency=currency,
                )
            except PaymentProviderError as exc:
                return RefundExecutionResult(
                    refund_id=None,
                    status="PENDING",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=str(exc),
                    provider_status="UNKNOWN",
                )

            if capture.status == "COMPLETED":
                # This is only a preflight status, never proof that a refund completed.
                if reconcile_before_refund:
                    return await self._reconcile_existing_refund(
                        client,
                        token,
                        order_id,
                        capture_id,
                        quantized,
                        currency,
                    )
            elif capture.status in ("PARTIALLY_REFUNDED", "REFUNDED"):
                existing = await self._reconcile_existing_refund(
                    client,
                    token,
                    order_id,
                    capture_id,
                    quantized,
                    currency,
                )
                if capture.status == "PARTIALLY_REFUNDED":
                    return RefundExecutionResult(
                        refund_id=existing.refund_id,
                        status="PENDING",
                        refunded_amount=existing.refunded_amount,
                        currency=existing.currency,
                        simulated=False,
                        failure_reason=(
                            "PayPal reports a partially refunded capture; no additional "
                            "automatic full refund was submitted."
                        ),
                        provider_status=existing.provider_status or capture.status,
                    )
                return existing
            else:
                return RefundExecutionResult(
                    refund_id=None,
                    status="PENDING",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "PayPal capture status "
                        f"'{capture.status}' does not authorize a refund request; "
                        "refund reconciliation remains pending."
                    ),
                    provider_status=capture.status,
                )

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
                    status="PENDING",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "Timed out waiting for PayPal's refund response; "
                        "the refund outcome is unknown and must be reconciled."
                    ),
                )
            except httpx.HTTPError as exc:
                return RefundExecutionResult(
                    refund_id=None,
                    status="PENDING",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "The PayPal refund request outcome is unknown after a transport error "
                        f"({type(exc).__name__}); it must be reconciled."
                    ),
                )

            if response.status_code not in (200, 201):
                uncertain = response.status_code >= 500 or response.status_code == 409
                return RefundExecutionResult(
                    refund_id=None,
                    status="PENDING" if uncertain else "FAILED",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "PayPal returned an ambiguous response to the refund request "
                        f"(HTTP {response.status_code}); it must be reconciled."
                        if uncertain
                        else f"PayPal rejected the refund request with HTTP {response.status_code}."
                    ),
                )

            try:
                payload = response.json()
            except ValueError as exc:
                return RefundExecutionResult(
                    refund_id=None,
                    status="PENDING",
                    refunded_amount=None,
                    currency=None,
                    simulated=False,
                    failure_reason=(
                        "PayPal accepted the refund request but returned unreadable JSON; "
                        f"the outcome is unknown ({type(exc).__name__})."
                    ),
                )

            return self._parse_refund_response(
                payload,
                expected_amount=quantized,
                expected_currency=currency,
                source="PayPal refund response",
            )

    # --- Authorization-hold settlement (Orders v2 authorize; Payments v2 void/capture) ----

    @staticmethod
    def _error_issues(response: httpx.Response) -> tuple[list[str], str]:
        """Return PayPal error issue codes and the error name from an error response."""
        try:
            payload = response.json()
        except ValueError:
            return [], ""
        if not isinstance(payload, dict):
            return [], ""
        details = payload.get("details")
        issues = [
            str(item.get("issue"))
            for item in (details if isinstance(details, list) else [])
            if isinstance(item, dict) and item.get("issue")
        ]
        name = payload.get("name")
        return issues, name if isinstance(name, str) else ""

    @classmethod
    def _parse_authorization(
        cls,
        payload: Any,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        if not isinstance(payload, dict):
            raise PaymentProviderError("PayPal returned a malformed order response.")
        order_id = str(payload.get("id", ""))
        order_status = str(payload.get("status", "UNKNOWN"))
        approval_url = cls._extract_approval_url(payload)

        authorization: Optional[dict[str, Any]] = None
        units = payload.get("purchase_units")
        if isinstance(units, list) and units and isinstance(units[0], dict):
            payments = units[0].get("payments")
            if isinstance(payments, dict):
                authorizations = payments.get("authorizations")
                if (
                    isinstance(authorizations, list)
                    and authorizations
                    and isinstance(authorizations[0], dict)
                ):
                    authorization = authorizations[0]

        if authorization is None:
            return _evaluate_authorization(
                order_id=order_id,
                order_status=order_status,
                authorization_id=None,
                authorization_status=None,
                amount=None,
                currency=None,
                expected_amount=expected_amount,
                expected_currency=expected_currency,
                simulated=False,
                approval_url=approval_url,
            )
        amount, currency = _parse_money(authorization.get("amount"))
        raw_status = authorization.get("status")
        return _evaluate_authorization(
            order_id=order_id,
            order_status=order_status,
            authorization_id=str(authorization.get("id", "")).strip() or None,
            authorization_status=raw_status.strip().upper() if isinstance(raw_status, str) else None,
            amount=amount,
            currency=currency,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            simulated=False,
            approval_url=approval_url,
        )

    async def authorize_order(
        self,
        *,
        order_id: str,
        request_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            try:
                response = await client.post(
                    f"/v2/checkout/orders/{quote(order_id, safe='')}/authorize",
                    json={},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": request_id,
                        "Prefer": "return=representation",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise PaymentProviderTimeoutError(
                    f"Timed out authorizing PayPal order {order_id}"
                ) from exc
            except httpx.HTTPError as exc:
                raise PaymentProviderError(
                    f"Transport error authorizing PayPal order {order_id} ({type(exc).__name__})"
                ) from exc

        if response.status_code in (200, 201):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return self._parse_authorization(payload, expected_amount, expected_currency)
        if response.status_code in (200, 201, 202):
            # Accepted, or the body was unreadable: read the authoritative order state.
            return await self.verify_authorization(
                order_id=order_id,
                expected_amount=expected_amount,
                expected_currency=expected_currency,
            )

        issues, error_name = self._error_issues(response)
        if "ORDER_ALREADY_AUTHORIZED" in issues:
            return await self.verify_authorization(
                order_id=order_id,
                expected_amount=expected_amount,
                expected_currency=expected_currency,
            )
        if "ORDER_NOT_APPROVED" in issues or "PAYER_ACTION_REQUIRED" in issues:
            current = await self.verify_authorization(
                order_id=order_id,
                expected_amount=expected_amount,
                expected_currency=expected_currency,
            )
            raise PaymentApprovalRequiredError(
                f"PayPal order {order_id} has not been approved by the payer yet.",
                approval_url=current.approval_url,
            )
        raise PaymentProviderError(
            f"PayPal order authorization failed with HTTP {response.status_code} "
            f"({error_name}: {','.join(issues)})"
        )

    async def verify_authorization(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        expected_currency: str,
    ) -> AuthorizationResult:
        async with self._build_client() as client:
            token = await self._get_access_token(client)
            try:
                response = await client.get(
                    f"/v2/checkout/orders/{quote(order_id, safe='')}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.TimeoutException as exc:
                raise PaymentProviderTimeoutError(
                    f"Timed out looking up PayPal order {order_id}"
                ) from exc
            except httpx.HTTPError as exc:
                raise PaymentProviderError(
                    f"Transport error looking up PayPal order {order_id} ({type(exc).__name__})"
                ) from exc
        if response.status_code != 200:
            raise PaymentProviderError(
                f"PayPal order lookup failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentProviderError("PayPal order lookup returned unreadable JSON.") from exc
        return self._parse_authorization(payload, expected_amount, expected_currency)

    async def _get_authorization_status(
        self,
        client: httpx.AsyncClient,
        token: str,
        authorization_id: str,
    ) -> Optional[str]:
        """Return the authorization's current status, or None if it cannot be read."""
        try:
            response = await client.get(
                f"/v2/payments/authorizations/{quote(authorization_id, safe='')}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        status = payload.get("status") if isinstance(payload, dict) else None
        return status.strip().upper() if isinstance(status, str) and status.strip() else None

    async def void_authorization(
        self,
        *,
        authorization_id: str,
        order_id: Optional[str],
        request_id: str,
    ) -> SettlementResult:
        async with self._build_client() as client:
            try:
                token = await self._get_access_token(client)
            except PaymentProviderError as exc:
                return SettlementResult(
                    status="FAILED",
                    simulated=False,
                    failure_reason=f"PayPal token acquisition failed before voiding ({type(exc).__name__}).",
                )
            try:
                response = await client.post(
                    f"/v2/payments/authorizations/{quote(authorization_id, safe='')}/void",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": request_id,
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                return await self._reconcile_void(
                    client, token, authorization_id, reason=f"a transport error ({type(exc).__name__})"
                )

            # 204 No Content by default; 200 only with Prefer: return=representation.
            if response.status_code in (200, 204):
                return SettlementResult(status="VOIDED", simulated=False, provider_status="VOIDED")
            if response.status_code == 409:
                return SettlementResult(
                    status="PENDING",
                    simulated=False,
                    failure_reason="A previous void request for this authorization is still in progress.",
                )
            issues, error_name = self._error_issues(response)
            return await self._reconcile_void(
                client,
                token,
                authorization_id,
                reason=f"HTTP {response.status_code} ({error_name}: {','.join(issues)})",
            )

    async def _reconcile_void(
        self,
        client: httpx.AsyncClient,
        token: str,
        authorization_id: str,
        *,
        reason: str,
    ) -> SettlementResult:
        status = await self._get_authorization_status(client, token, authorization_id)
        if status == "VOIDED":
            return SettlementResult(status="VOIDED", simulated=False, provider_status="VOIDED")
        if status is None:
            return SettlementResult(
                status="PENDING",
                simulated=False,
                failure_reason=f"Void outcome is unknown after {reason}; reconcile before retrying.",
            )
        return SettlementResult(
            status="FAILED",
            simulated=False,
            provider_status=status,
            failure_reason=f"PayPal did not void the authorization after {reason} (status {status}).",
        )

    async def capture_authorization(
        self,
        *,
        authorization_id: str,
        order_id: Optional[str],
        amount: Decimal,
        currency: str,
        request_id: str,
        note: str,
    ) -> SettlementResult:
        quantized = amount.quantize(Decimal("0.01"))
        body = {
            "amount": {"value": f"{quantized:.2f}", "currency_code": currency.upper()},
            "final_capture": True,
            "note_to_payer": note[:255],
        }
        async with self._build_client() as client:
            try:
                token = await self._get_access_token(client)
            except PaymentProviderError as exc:
                return SettlementResult(
                    status="FAILED",
                    simulated=False,
                    failure_reason=f"PayPal token acquisition failed before capture ({type(exc).__name__}).",
                )
            try:
                response = await client.post(
                    f"/v2/payments/authorizations/{quote(authorization_id, safe='')}/capture",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "PayPal-Request-Id": request_id,
                        "Prefer": "return=representation",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                return await self._reconcile_authorization_capture(
                    client,
                    token,
                    order_id,
                    quantized,
                    currency,
                    reason=f"a transport error ({type(exc).__name__})",
                )

            if response.status_code in (200, 201):
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    return self._parse_capture_settlement(payload, quantized, currency)
                return await self._reconcile_authorization_capture(
                    client, token, order_id, quantized, currency, reason="an unreadable capture response"
                )
            if response.status_code == 409:
                return SettlementResult(
                    status="PENDING",
                    simulated=False,
                    failure_reason="A previous capture request for this authorization is still in progress.",
                )
            issues, error_name = self._error_issues(response)
            if "AUTHORIZATION_EXPIRED" in issues:
                return SettlementResult(
                    status="FAILED",
                    simulated=False,
                    provider_status="EXPIRED",
                    failure_reason="The PayPal authorization expired before it could be captured.",
                )
            return await self._reconcile_authorization_capture(
                client,
                token,
                order_id,
                quantized,
                currency,
                reason=f"HTTP {response.status_code} ({error_name}: {','.join(issues)})",
                unknown_status="PENDING" if response.status_code >= 500 else "FAILED",
            )

    @staticmethod
    def _parse_capture_settlement(
        payload: dict[str, Any],
        expected_amount: Decimal,
        expected_currency: str,
    ) -> SettlementResult:
        capture_id = str(payload.get("id", "")).strip() or None
        raw_status = payload.get("status")
        status = raw_status.strip().upper() if isinstance(raw_status, str) else None
        amount, currency = _parse_money(payload.get("amount"))
        if status == "COMPLETED" and capture_id:
            if amount != expected_amount or currency != expected_currency.upper():
                return SettlementResult(
                    status="PENDING",
                    simulated=False,
                    capture_id=capture_id,
                    amount=amount,
                    currency=currency,
                    provider_status=status,
                    failure_reason="Captured amount or currency does not match the deposit; manual review required.",
                )
            return SettlementResult(
                status="CAPTURED",
                simulated=False,
                capture_id=capture_id,
                amount=amount,
                currency=currency,
                provider_status=status,
            )
        if status == "PENDING":
            return SettlementResult(
                status="PENDING",
                simulated=False,
                capture_id=capture_id,
                amount=amount,
                currency=currency,
                provider_status=status,
                failure_reason="PayPal reports the capture as PENDING settlement.",
            )
        return SettlementResult(
            status="FAILED",
            simulated=False,
            capture_id=capture_id,
            provider_status=status,
            failure_reason=f"PayPal capture status was '{status}'.",
        )

    async def _reconcile_authorization_capture(
        self,
        client: httpx.AsyncClient,
        token: str,
        order_id: Optional[str],
        expected_amount: Decimal,
        currency: str,
        *,
        reason: str,
        unknown_status: str = "PENDING",
    ) -> SettlementResult:
        """After an ambiguous capture response, look for a completed capture on the order."""
        if order_id:
            try:
                response = await client.get(
                    f"/v2/checkout/orders/{quote(order_id, safe='')}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
                payload = response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                payload = None
            units = payload.get("purchase_units") if isinstance(payload, dict) else None
            if isinstance(units, list) and units and isinstance(units[0], dict):
                payments = units[0].get("payments")
                captures = payments.get("captures") if isinstance(payments, dict) else None
                for capture in captures if isinstance(captures, list) else []:
                    if isinstance(capture, dict):
                        settled = self._parse_capture_settlement(capture, expected_amount, currency)
                        if settled.status == "CAPTURED":
                            return settled
        return SettlementResult(
            status=unknown_status,
            simulated=False,
            failure_reason=(
                f"Capture outcome is unknown after {reason}; reconcile before retrying."
                if unknown_status == "PENDING"
                else f"PayPal rejected the capture: {reason}."
            ),
        )
