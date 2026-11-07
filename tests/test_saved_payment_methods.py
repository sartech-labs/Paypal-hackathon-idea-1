"""Saved payment methods: consent once, then the agent pays with no one present.

Also covers the order of operations for agent purchases: the order is created with no payment
method (nothing charged), and the method is attached only at capture, after the agent's
mandate and purchase review allow the purchase.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.services.payment_service import PayPalSandboxProvider, extract_saved_method
from tests.conftest import complete_onboarding_flow  # noqa: F401  (fixture helpers)


def _deposit(c: TestClient, crawler: str, **extra: Any) -> dict:
    chg = c.get("/gateway/news", headers={"X-Crawler-Id": crawler}).json()
    res = c.post("/deposits/create", json={"challenge_id": chg["challenge_id"], "crawler_id": crawler, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def test_first_payment_saves_the_method_and_later_payments_reuse_it(test_settings: Settings) -> None:
    with TestClient(create_app(settings=test_settings)) as c:
        first = _deposit(c, "crawler-wallet", save_payment_method=True)
        pm = first["payment_method_id"]
        assert pm and pm.startswith("pm_") and "1111" in first["payment_method_label"]

        again = _deposit(c, "crawler-wallet", payment_method_id=pm)
        assert again["payment_method_id"] == pm

        # A commerce order paid with the saved method: created unpaid, charged at capture.
        order = c.post("/commerce/checkout", json={"agent_id": "agent-wallet", "offer_type": "access", "item_id": "agent-economy-video", "payment_method_id": pm})
        assert order.status_code == 201, order.text
        assert order.json()["status"] != "PAID" and order.json()["approval_url"] is None
        paid = c.post(f"/commerce/orders/{order.json()['order_id']}/capture", json={"agent_id": "agent-wallet"}).json()
        assert paid["status"] == "PAID" and paid["payment_method_id"] == pm

        # The vault token stays server-side; responses carry only the opaque ID and a label.
        assert "mock_vault" not in json.dumps(first) and "mock_vault" not in json.dumps(paid)

        bad = c.post("/commerce/checkout", json={"agent_id": "agent-wallet", "offer_type": "access", "item_id": "agent-economy-video", "payment_method_id": "pm_nope"})
        assert bad.status_code == 404


def test_extract_saved_method_reads_paypal_vault_ids() -> None:
    src = {"card": {"brand": "VISA", "last_digits": "1111", "attributes": {"vault": {"id": "9sb8", "status": "VAULTED"}}}}
    assert extract_saved_method(src) == {"kind": "card", "token": "9sb8", "label": "Visa •••• 1111"}
    assert extract_saved_method({"card": {"last_digits": "1111"}}) is None


async def test_sandbox_agent_orders_charge_only_at_capture(test_settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path == "/v2/checkout/orders":
            return httpx.Response(201, json={"id": "O1", "status": "CREATED", "links": []})
        if path.endswith("/confirm-payment-source"):
            return httpx.Response(200, json={"id": "O1", "status": "APPROVED"})
        if path.endswith("/capture"):
            return httpx.Response(201, json={
                "id": "O1", "status": "COMPLETED",
                "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "attributes": {"vault": {"id": "VAULT1", "status": "VAULTED"}}}},
                "purchase_units": [{"payments": {"captures": [{"id": "C1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "2.00"}}]}}],
            })
        return httpx.Response(404, json={})

    settings = test_settings.model_copy(update={"payment_mode": "paypal_sandbox", "paypal_sandbox_autopay": True})
    provider = PayPalSandboxProvider(settings, transport=httpx.MockTransport(handler))

    created = await provider.create_order(deposit_id="ord_1", amount=Decimal("2.00"), currency="USD", request_id="r1", description="x", defer_payment=True)
    assert "payment_source" not in json.loads(requests[-1].content)  # nothing to charge yet
    assert created.status == "CREATED"

    await provider.confirm_payment_source(order_id="O1", request_id="r2", save_method=True)
    source = json.loads(requests[-1].content)["payment_source"]["card"]
    assert source["attributes"]["vault"]["store_in_vault"] == "ON_SUCCESS"

    captured = await provider.capture_order(order_id="O1", request_id="r3", expected_amount=Decimal("2.00"), expected_currency="USD")
    assert captured.verified and captured.saved_method == {"kind": "card", "token": "VAULT1", "label": "Visa •••• 1111"}

    await provider.confirm_payment_source(order_id="O2", request_id="r4", payment_token="card:VAULT1")
    assert json.loads(requests[-1].content)["payment_source"] == {"card": {"vault_id": "VAULT1"}}


def test_declined_agent_order_is_never_charged(test_settings: Settings) -> None:
    with TestClient(create_app(settings=test_settings)) as c:
        provider = c.app.state.container.payment_provider
        order = c.post("/commerce/checkout", json={"agent_id": "agent-decline", "offer_type": "access", "item_id": "agent-economy-video", "save_payment_method": True}).json()
        cancel = c.post(f"/commerce/orders/{order['order_id']}/cancel", json={"agent_id": "agent-decline"})
        assert cancel.status_code == 200
        mock_order = provider._orders[order["provider_order_id"]]
        assert mock_order["capture_id"] is None
