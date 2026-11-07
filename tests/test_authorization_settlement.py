"""Tests for authorization-hold settlement: hold on deposit, void if compliant, capture if forfeited."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.payment_service import (
    MockPaymentProvider,
    PaymentApprovalRequiredError,
    PayPalSandboxProvider,
    SettlementResult,
)
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow

ORDER_ID = "ORD_AUTH_1"
AUTH_ID = "AUTH_1"


@pytest.fixture
def auth_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(update={"deposit_settlement": "authorization"})


def _client(settings: Settings, clock: FakeClock, provider: Any = None) -> httpx.AsyncClient:
    app = create_app(
        settings=settings,
        payment_provider=provider,
        rate_limiter=SlidingWindowRateLimiter(clock=clock),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# --- End-to-end in mock mode ------------------------------------------------------------


@pytest.mark.asyncio
async def test_compliant_session_releases_hold_without_capture(
    auth_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(auth_settings, fake_clock) as client:
        assert (await client.get("/health")).json()["deposit_settlement"] == "authorization"
        _, deposit, session = await complete_onboarding_flow(client, "crawler-hold-ok")
        assert deposit["settlement_mode"] == "authorization"
        assert deposit["payment_status"] == "AUTHORIZED"
        assert deposit["deposit_state"] == "AUTHORIZED"
        assert deposit["provider_authorization_id"].startswith("mock_auth_")
        assert deposit["provider_capture_id"] is None

        headers = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/news", headers=headers)).status_code == 200

        done = await client.post("/sessions/complete", headers=headers)
        assert done.status_code == 200
        body = done.json()
        assert body["refund_eligible"] is True
        final = body["deposit"]
        assert final["deposit_state"] == "RELEASED"
        assert final["payment_status"] == "VOIDED"
        assert final["policy_decision"] == "ELIGIBLE_FOR_REFUND"
        assert final["refund_status"] == "NOT_APPLICABLE_RELEASED"
        assert final["provider_capture_id"] is None
        assert final["provider_refund_id"] is None
        assert final["released_at"]

        # Completing again is idempotent and does not touch the provider state.
        again = (await client.post("/sessions/complete", headers=headers)).json()["deposit"]
        assert again["deposit_state"] == "RELEASED"

        # Payment events precede the session, so read them by deposit with the admin token.
        events = (
            await client.get(
                "/audit/events",
                params={"deposit_id": final["deposit_id"]},
                headers={"X-Admin-Token": "test-admin-secret-token-999"},
            )
        ).json()
        types = [e["event_type"] for e in events]
        assert "PAYMENT_AUTHORIZED" in types and "DEPOSIT_RELEASED" in types
        assert "DEPOSIT_REFUNDED" not in types

        explanation = (
            await client.get(f"/deposits/{final['deposit_id']}/explanation", headers=headers)
        ).json()
        assert "hold was released" in explanation["summary"]


@pytest.mark.asyncio
async def test_violating_session_captures_hold(auth_settings: Settings, fake_clock: FakeClock) -> None:
    async with _client(auth_settings, fake_clock) as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-hold-bad")
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/admin", headers=headers)).status_code == 403

        body = (await client.post("/sessions/complete", headers=headers)).json()
        assert body["refund_eligible"] is False
        final = body["deposit"]
        assert final["deposit_state"] == "FORFEITED"
        assert final["payment_status"] == "CAPTURED"
        assert final["policy_decision"] == "FORFEITED_BY_POLICY"
        assert final["refund_status"] == "NOT_APPLICABLE_FORFEITED"
        assert final["provider_capture_id"].startswith("mock_cap_")
        assert final["captured_amount"] == "5.00"
        assert "authorization hold captured" in final["decision_reason"]

        # The forfeited crawler cannot claw back the deposit through the refund endpoint.
        refund = await client.post(f"/deposits/{final['deposit_id']}/refund", headers=headers)
        assert refund.json()["deposit_state"] == "FORFEITED"


@pytest.mark.asyncio
async def test_existing_refund_mode_deposits_keep_their_mode(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(test_settings, fake_clock) as client:
        _, deposit, session = await complete_onboarding_flow(client, "crawler-refund-mode")
        assert deposit["settlement_mode"] == "refund"
        assert deposit["payment_status"] == "CAPTURED"
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        final = (await client.post("/sessions/complete", headers=headers)).json()["deposit"]
        assert final["deposit_state"] == "REFUNDED"
        assert final["refund_status"] == "REFUNDED"


class FlakySettlementProvider(MockPaymentProvider):
    """Mock provider whose first void/capture attempt returns an unconfirmed outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.void_failures = 1
        self.capture_failures = 1

    async def void_authorization(self, **kwargs: Any) -> SettlementResult:
        if self.void_failures:
            self.void_failures -= 1
            return SettlementResult(status="PENDING", simulated=True, failure_reason="Simulated void timeout")
        return await super().void_authorization(**kwargs)

    async def capture_authorization(self, **kwargs: Any) -> SettlementResult:
        if self.capture_failures:
            self.capture_failures -= 1
            return SettlementResult(status="FAILED", simulated=True, failure_reason="Simulated capture error")
        return await super().capture_authorization(**kwargs)


@pytest.mark.asyncio
async def test_unconfirmed_void_keeps_decision_and_retry_releases(
    auth_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(auth_settings, fake_clock, FlakySettlementProvider()) as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-void-retry")
        headers = {"Authorization": f"Bearer {session['access_token']}"}

        first = (await client.post("/sessions/complete", headers=headers)).json()["deposit"]
        assert first["deposit_state"] == "AUTHORIZED"
        assert first["policy_decision"] == "ELIGIBLE_FOR_REFUND"
        assert "Simulated void timeout" in first["failure_reason"]

        retry = (await client.post("/sessions/complete", headers=headers)).json()["deposit"]
        assert retry["deposit_state"] == "RELEASED"
        assert retry["failure_reason"] is None


@pytest.mark.asyncio
async def test_failed_forfeit_capture_keeps_forfeiture_and_retry_captures(
    auth_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(auth_settings, fake_clock, FlakySettlementProvider()) as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-capture-retry")
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/private/customer-data", headers=headers)).status_code == 403

        first = (await client.post("/sessions/complete", headers=headers)).json()
        assert first["refund_eligible"] is False
        assert first["deposit"]["deposit_state"] == "AUTHORIZED"
        assert first["deposit"]["policy_decision"] == "FORFEITED_BY_POLICY"

        retry = (
            await client.post(f"/deposits/{first['deposit']['deposit_id']}/refund", headers=headers)
        ).json()
        assert retry["deposit_state"] == "FORFEITED"
        assert retry["payment_status"] == "CAPTURED"


# --- PayPal Sandbox provider against a mocked PayPal ------------------------------------


def _order_payload(*, authorized: bool, auth_status: str, auth_amount: str, captures: Optional[list[Any]] = None) -> dict[str, Any]:
    unit: dict[str, Any] = {"reference_id": "default"}
    if authorized:
        unit["payments"] = {
            "authorizations": [
                {"id": AUTH_ID, "status": auth_status, "amount": {"value": auth_amount, "currency_code": "USD"}}
            ],
            "captures": captures or [],
        }
    return {
        "id": ORDER_ID,
        "status": "COMPLETED" if authorized else "CREATED",
        "purchase_units": [unit],
        "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER_ID}"}],
    }


def _paypal(
    *,
    log: Optional[list[dict[str, Any]]] = None,
    require_approval: bool = False,
    auth_status: str = "CREATED",
    auth_amount: str = "5.00",
    void_http: int = 204,
    void_timeout: bool = False,
    auth_lookup_status: Optional[str] = "VOIDED",
    capture_http: int = 201,
    capture_status: str = "COMPLETED",
    capture_issue: Optional[str] = None,
    capture_timeout: bool = False,
    order_captures: Optional[list[Any]] = None,
) -> httpx.MockTransport:
    state = {"authorized": False}
    calls = log if log is not None else []

    async def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        is_json = request.headers.get("content-type", "").startswith("application/json")
        body = json.loads(request.content) if request.content and is_json else None
        calls.append({"method": method, "path": path, "headers": dict(request.headers), "body": body})

        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path == "/v2/checkout/orders" and method == "POST":
            return httpx.Response(201, json=_order_payload(authorized=False, auth_status=auth_status, auth_amount=auth_amount))
        if path == f"/v2/checkout/orders/{ORDER_ID}/authorize":
            if require_approval:
                return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
            state["authorized"] = True
            return httpx.Response(201, json=_order_payload(authorized=True, auth_status=auth_status, auth_amount=auth_amount))
        if path == f"/v2/checkout/orders/{ORDER_ID}" and method == "GET":
            return httpx.Response(
                200,
                json=_order_payload(
                    authorized=state["authorized"] or order_captures is not None,
                    auth_status=auth_status,
                    auth_amount=auth_amount,
                    captures=order_captures,
                ),
            )
        if path == f"/v2/payments/authorizations/{AUTH_ID}/void":
            if void_timeout:
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if void_http in (200, 204):
                return httpx.Response(void_http)
            return httpx.Response(void_http, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "CANNOT_BE_VOIDED"}]})
        if path == f"/v2/payments/authorizations/{AUTH_ID}" and method == "GET":
            if auth_lookup_status is None:
                return httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"})
            return httpx.Response(200, json={"id": AUTH_ID, "status": auth_lookup_status})
        if path == f"/v2/payments/authorizations/{AUTH_ID}/capture":
            if capture_timeout:
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if capture_http in (200, 201):
                return httpx.Response(
                    capture_http,
                    json={"id": "CAP_1", "status": capture_status, "amount": {"value": "5.00", "currency_code": "USD"}},
                )
            details = [{"issue": capture_issue}] if capture_issue else []
            return httpx.Response(capture_http, json={"name": "ERROR", "details": details})
        return httpx.Response(404, json={"name": "NOT_FOUND", "path": path})

    return httpx.MockTransport(handler)


def _sandbox(settings: Settings, transport: httpx.MockTransport) -> PayPalSandboxProvider:
    return PayPalSandboxProvider(settings.model_copy(update={"payment_mode": "paypal_sandbox"}), transport=transport)


@pytest.mark.asyncio
async def test_sandbox_create_order_uses_authorize_intent(test_settings: Settings) -> None:
    log: list[dict[str, Any]] = []
    provider = _sandbox(test_settings, _paypal(log=log))
    result = await provider.create_order(
        deposit_id="dep_1", amount=Decimal("5.00"), currency="USD", request_id="ord_req_dep_1",
        description="test", intent="AUTHORIZE",
    )
    assert result.order_id == ORDER_ID
    order_call = next(c for c in log if c["path"] == "/v2/checkout/orders")
    assert order_call["body"]["intent"] == "AUTHORIZE"


@pytest.mark.asyncio
async def test_sandbox_authorize_order_verifies_hold(test_settings: Settings) -> None:
    log: list[dict[str, Any]] = []
    provider = _sandbox(test_settings, _paypal(log=log))
    result = await provider.authorize_order(
        order_id=ORDER_ID, request_id="auth_req_dep_1", expected_amount=Decimal("5.00"), expected_currency="USD"
    )
    assert result.verified is True
    assert result.authorization_id == AUTH_ID and result.authorization_status == "CREATED"
    call = next(c for c in log if c["path"].endswith("/authorize"))
    assert call["headers"]["paypal-request-id"] == "auth_req_dep_1"
    assert call["headers"]["prefer"] == "return=representation"


@pytest.mark.asyncio
async def test_sandbox_authorize_requires_payer_approval(test_settings: Settings) -> None:
    provider = _sandbox(test_settings, _paypal(require_approval=True))
    with pytest.raises(PaymentApprovalRequiredError) as exc_info:
        await provider.authorize_order(
            order_id=ORDER_ID, request_id="r", expected_amount=Decimal("5.00"), expected_currency="USD"
        )
    assert exc_info.value.approval_url and ORDER_ID in exc_info.value.approval_url


@pytest.mark.asyncio
@pytest.mark.parametrize(("auth_status", "auth_amount"), [("CREATED", "0.50"), ("PENDING", "5.00"), ("DENIED", "5.00")])
async def test_sandbox_authorization_must_be_created_and_match(
    test_settings: Settings, auth_status: str, auth_amount: str
) -> None:
    provider = _sandbox(test_settings, _paypal(auth_status=auth_status, auth_amount=auth_amount))
    result = await provider.authorize_order(
        order_id=ORDER_ID, request_id="r", expected_amount=Decimal("5.00"), expected_currency="USD"
    )
    assert result.verified is False
    assert result.failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"void_http": 204}, "VOIDED"),
        ({"void_http": 200}, "VOIDED"),
        ({"void_http": 409}, "PENDING"),
        ({"void_http": 422, "auth_lookup_status": "VOIDED"}, "VOIDED"),
        ({"void_http": 422, "auth_lookup_status": "CAPTURED"}, "FAILED"),
        ({"void_timeout": True, "auth_lookup_status": None}, "PENDING"),
        ({"void_timeout": True, "auth_lookup_status": "VOIDED"}, "VOIDED"),
    ],
)
async def test_sandbox_void_authorization_outcomes(
    test_settings: Settings, options: dict[str, Any], expected: str
) -> None:
    log: list[dict[str, Any]] = []
    provider = _sandbox(test_settings, _paypal(log=log, **options))
    result = await provider.void_authorization(authorization_id=AUTH_ID, order_id=ORDER_ID, request_id="void_req_dep_1")
    assert result.status == expected
    void_call = next(c for c in log if c["path"].endswith("/void"))
    assert void_call["headers"]["paypal-request-id"] == "void_req_dep_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, "CAPTURED"),
        ({"capture_status": "PENDING"}, "PENDING"),
        ({"capture_status": "DECLINED"}, "FAILED"),
        ({"capture_http": 409}, "PENDING"),
        ({"capture_http": 422, "capture_issue": "AUTHORIZATION_EXPIRED"}, "FAILED"),
        (
            {
                "capture_http": 422,
                "capture_issue": "PREVIOUSLY_CAPTURED",
                "order_captures": [{"id": "CAP_PREV", "status": "COMPLETED", "amount": {"value": "5.00", "currency_code": "USD"}}],
            },
            "CAPTURED",
        ),
        (
            {
                "capture_timeout": True,
                "order_captures": [{"id": "CAP_LATE", "status": "COMPLETED", "amount": {"value": "5.00", "currency_code": "USD"}}],
            },
            "CAPTURED",
        ),
        ({"capture_timeout": True}, "PENDING"),
        ({"capture_http": 500}, "PENDING"),
    ],
)
async def test_sandbox_capture_authorization_outcomes(
    test_settings: Settings, options: dict[str, Any], expected: str
) -> None:
    log: list[dict[str, Any]] = []
    provider = _sandbox(test_settings, _paypal(log=log, **options))
    result = await provider.capture_authorization(
        authorization_id=AUTH_ID, order_id=ORDER_ID, amount=Decimal("5.00"), currency="USD",
        request_id="cap_req_dep_1", note="forfeited",
    )
    assert result.status == expected
    capture_call = next(c for c in log if c["path"].endswith("/capture"))
    assert capture_call["headers"]["paypal-request-id"] == "cap_req_dep_1"
    assert capture_call["body"]["final_capture"] is True
    assert capture_call["body"]["amount"] == {"value": "5.00", "currency_code": "USD"}


@pytest.mark.asyncio
async def test_sandbox_end_to_end_release_through_the_app(
    auth_settings: Settings, fake_clock: FakeClock
) -> None:
    log: list[dict[str, Any]] = []
    settings = auth_settings.model_copy(update={"payment_mode": "paypal_sandbox"})
    provider = PayPalSandboxProvider(settings, transport=_paypal(log=log))
    async with _client(settings, fake_clock, provider) as client:
        _, deposit, session = await complete_onboarding_flow(client, "crawler-sandbox-hold")
        assert deposit["payment_status"] == "AUTHORIZED"
        assert deposit["provider_authorization_id"] == AUTH_ID
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/news", headers=headers)).status_code == 200
        final = (await client.post("/sessions/complete", headers=headers)).json()["deposit"]
        assert final["deposit_state"] == "RELEASED"

    paths = [c["path"] for c in log]
    assert f"/v2/checkout/orders/{ORDER_ID}/authorize" in paths
    assert f"/v2/payments/authorizations/{AUTH_ID}/void" in paths
    assert not any(p.endswith("/refund") or p.endswith(f"/{AUTH_ID}/capture") for p in paths)
