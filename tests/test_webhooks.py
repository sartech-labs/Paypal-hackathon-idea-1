"""PayPal webhooks (signature-verified, idempotent) and publisher refunds and tracking."""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.services.payment_service import MOCK_WEBHOOK_SIGNATURE, PayPalSandboxProvider

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}
SIGNED = {"PayPal-Transmission-Sig": MOCK_WEBHOOK_SIGNATURE}
AGENT = "agent-hook"


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings=test_settings.model_copy(update={"dashboard_public": True}))) as c:
        yield c


def _event(event_type: str, resource: dict[str, Any], event_id: str) -> dict[str, Any]:
    return {"id": event_id, "event_type": event_type, "resource": resource}


def _post(client: TestClient, event: dict[str, Any], headers: dict[str, str] = SIGNED) -> Any:
    return client.post("/webhooks/paypal", content=json.dumps(event), headers={"Content-Type": "application/json", **headers})


def _order(client: TestClient, **body: Any) -> dict[str, Any]:
    payload = {"agent_id": AGENT, "offer_type": "access", "item_id": "agent-economy-video", **body}
    return client.post("/commerce/checkout", json=payload).json()


def _paid_order(client: TestClient, **body: Any) -> dict[str, Any]:
    order = _order(client, **body)
    return client.post(f"/commerce/orders/{order['order_id']}/capture", json={"agent_id": AGENT}).json()


def test_unsigned_or_malformed_events_are_rejected(client: TestClient) -> None:
    event = _event("PAYMENT.CAPTURE.COMPLETED", {"custom_id": "ord_x"}, "WH-1")
    assert _post(client, event, headers={}).status_code == 401
    assert _post(client, event, headers={"PayPal-Transmission-Sig": "forged"}).status_code == 401
    assert client.post("/webhooks/paypal", content="not json").status_code == 400
    assert _post(client, {"hello": "world"}).status_code == 400


def test_capture_webhook_confirms_with_paypal_and_the_agent_still_gets_its_token(client: TestClient) -> None:
    order = _order(client)
    # Simulate the payer having paid at PayPal: the mock order is captured, but our record is not.
    provider = client.app.state.container.payment_provider
    provider._orders[order["provider_order_id"]].update(capture_id="mock_cap_hook", capture_status="COMPLETED", status="COMPLETED")
    resp = _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"id": "mock_cap_hook", "custom_id": order["order_id"]}, "WH-2"))
    assert resp.json()["outcome"] == "marked_paid"
    assert _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"custom_id": order["order_id"]}, "WH-2")).json()["outcome"] == "duplicate"
    status = client.get(f"/commerce/orders/{order['order_id']}", headers={"X-Agent-Id": AGENT}).json()
    assert status["status"] == "PAID" and status["confirmed_by_webhook"] is True
    paid = client.post(f"/commerce/orders/{order['order_id']}/capture", json={"agent_id": AGENT}).json()
    assert paid["access_token"]  # issued on the agent's first call after the webhook
    assert client.get("/paid/media/video/agent-economy-briefing", headers={"Authorization": f"Bearer {paid['access_token']}"}).status_code == 200


def test_capture_webhook_does_not_trust_the_event_body(client: TestClient) -> None:
    order = _order(client)  # never captured at the (mock) provider
    resp = _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"custom_id": order["order_id"]}, "WH-3"))
    assert resp.json()["outcome"] == "not_verified"
    assert client.get(f"/commerce/orders/{order['order_id']}", headers={"X-Agent-Id": AGENT}).json()["status"] == "CREATED"


def test_refund_webhook_ends_access_and_dispute_is_recorded(client: TestClient) -> None:
    paid = _paid_order(client)
    capture = paid["provider_capture_id"]
    dispute = {"dispute_id": "PP-D-1", "status": "OPEN", "disputed_transactions": [{"seller_transaction_id": capture}]}
    assert _post(client, _event("CUSTOMER.DISPUTE.CREATED", dispute, "WH-4")).json()["outcome"] == "dispute_recorded"
    up = [{"rel": "up", "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{capture}"}]
    # A refund PayPal does not know about is not trusted.
    assert _post(client, _event("PAYMENT.CAPTURE.REFUNDED", {"id": "REF-FORGED", "links": up}, "WH-5a")).json()["outcome"] == "not_verified"
    # A real refund (made at PayPal, for example from PayPal's own dashboard) is.
    import asyncio
    from decimal import Decimal

    real = asyncio.run(client.app.state.container.payment_provider.refund_capture(
        capture_id=capture, order_id=paid["provider_order_id"], amount=Decimal("2.00"), currency="USD", request_id="dash-refund", reason="dashboard",
    ))
    assert _post(client, _event("PAYMENT.CAPTURE.REFUNDED", {"id": real.refund_id, "links": up}, "WH-5")).json()["outcome"] == "marked_refunded"
    order = client.get(f"/commerce/orders/{paid['order_id']}", headers={"X-Agent-Id": AGENT}).json()
    assert order["status"] == "REFUNDED" and order["provider_refund_id"] == real.refund_id and order["dispute_status"] == "OPEN"
    denied = client.get("/paid/media/video/agent-economy-briefing", headers={"Authorization": f"Bearer {paid['access_token']}"})
    assert denied.status_code == 402 and denied.json()["reason_code"] == "ORDER_NOT_PAID"
    summary = client.get("/commerce/summary").json()
    assert summary["refunded_orders"] == 1 and summary["open_disputes"] == 1 and summary["webhook_events"] == 3


def test_subscription_webhooks_sync_status_and_count_renewals(client: TestClient) -> None:
    sub = client.post("/commerce/subscriptions", json={"agent_id": AGENT, "plan_id": "sentinel-pro-monthly"}).json()
    provider_id = sub["provider_subscription_id"]
    sale = {"id": "SALE-1", "billing_agreement_id": provider_id}
    assert _post(client, _event("PAYMENT.SALE.COMPLETED", sale, "WH-6")).json()["outcome"] == "payment_recorded"
    # Cancelled at PayPal (for example by the payer in their PayPal account).
    import asyncio

    asyncio.run(client.app.state.container.payment_provider.cancel_subscription(subscription_id=provider_id, reason="payer"))
    resp = _post(client, _event("BILLING.SUBSCRIPTION.CANCELLED", {"id": provider_id, "custom_id": sub["subscription_ref"]}, "WH-7"))
    assert resp.json()["outcome"] == "status_cancelled"
    current = client.get(f"/commerce/subscriptions/{sub['subscription_ref']}", headers={"X-Agent-Id": AGENT}).json()
    assert current["status"] == "CANCELLED" and current["payments_received"] == 1
    assert client.get("/paid/members/daily-feed", headers={"Authorization": f"Bearer {sub['access_token']}"}).status_code == 402


def test_publisher_refund_and_tracking(client: TestClient) -> None:
    paid = _paid_order(client)
    assert client.post(f"/commerce/orders/{paid['order_id']}/refund", json={}).status_code == 401
    refunded = client.post(f"/commerce/orders/{paid['order_id']}/refund", json={"reason": "goodwill"}, headers=ADMIN).json()
    assert refunded["status"] == "REFUNDED" and refunded["provider_refund_id"]
    assert client.post(f"/commerce/orders/{paid['order_id']}/capture", json={"agent_id": AGENT}).status_code == 409

    mugs = _paid_order(client, offer_type="product", item_id=None, items=[{"sku": "mug-sentinel", "quantity": 1}])
    tracked = client.post(f"/commerce/orders/{mugs['order_id']}/tracking", json={"tracking_number": "1Z999AA10123456784", "carrier": "UPS"}, headers=ADMIN).json()
    assert tracked["fulfillment_status"] == "SHIPPED" and tracked["tracking"][0]["carrier"] == "UPS"
    ebook = _paid_order(client, offer_type="product", item_id=None, items=[{"sku": "ebook-agent-economy", "quantity": 1}])
    assert client.post(f"/commerce/orders/{ebook['order_id']}/tracking", json={"tracking_number": "ABCD1234", "carrier": "UPS"}, headers=ADMIN).status_code == 409


async def test_sandbox_verifies_signature_with_paypal(test_settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        body = json.loads(request.content)
        return httpx.Response(200, json={"verification_status": "SUCCESS" if body["transmission_sig"] == "good" else "FAILURE"})

    settings = test_settings.model_copy(update={"payment_mode": "paypal_sandbox"})
    provider = PayPalSandboxProvider(settings, transport=httpx.MockTransport(handler))
    headers = {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT",
        "paypal-transmission-id": "T1",
        "paypal-transmission-time": "2026-11-07T10:00:00Z",
    }
    event = {"id": "WH-9", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}
    assert await provider.verify_webhook_signature(headers={**headers, "paypal-transmission-sig": "good"}, event=event, webhook_id="WH-ID") is True
    assert await provider.verify_webhook_signature(headers={**headers, "paypal-transmission-sig": "bad"}, event=event, webhook_id="WH-ID") is False
    sent = json.loads(seen[1].content)
    assert sent["webhook_id"] == "WH-ID" and sent["webhook_event"] == event and sent["cert_url"].startswith("https://api.sandbox.paypal.com")
    # Without a configured webhook ID, or with missing headers, nothing verifies (and PayPal is not called).
    calls = len(seen)
    assert await provider.verify_webhook_signature(headers={**headers, "paypal-transmission-sig": "good"}, event=event, webhook_id="") is False
    assert await provider.verify_webhook_signature(headers={"paypal-transmission-sig": "good"}, event=event, webhook_id="WH-ID") is False
    assert len(seen) == calls
