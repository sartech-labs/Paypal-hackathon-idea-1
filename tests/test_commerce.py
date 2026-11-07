"""Agent commerce: offer catalog, pay-per-access, products, subscriptions, paid content."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.services.payment_service import OrderLineItem, PayPalSandboxProvider, PaymentProviderError

VIDEO = "/paid/media/video/agent-economy-briefing"


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    settings = test_settings.model_copy(update={"dashboard_public": True})
    with TestClient(create_app(settings=settings)) as c:
        yield c


def _buy_video(client: TestClient, agent: str = "agent-alpha") -> dict[str, Any]:
    order = client.post("/commerce/checkout", json={"agent_id": agent, "offer_type": "access", "item_id": "agent-economy-video"})
    assert order.status_code == 201
    paid = client.post(f"/commerce/orders/{order.json()['order_id']}/capture", json={"agent_id": agent})
    assert paid.status_code == 200
    return paid.json()


def test_catalog_lists_every_offer_type(client: TestClient) -> None:
    catalog = client.get("/.well-known/agent-commerce.json").json()
    offers = catalog["offers"]
    assert catalog["protocol"] == "agenttrust/1.0"
    assert catalog["payments"]["processor"] == "PayPal"
    assert {t["tier_id"] for t in offers["crawl_deposits"]["tiers"]} == {"premium", "standard", "public"}
    assert {i["item_id"] for i in offers["pay_per_access"]} >= {"agent-economy-video", "agent-traffic-dataset"}
    assert offers["subscriptions"][0]["plan_id"] == "sentinel-pro-monthly"
    assert offers["subscriptions"][0]["requires_payer_approval"] is True
    assert {p["sku"] for p in offers["products"]} == {"ebook-agent-economy", "print-annual-report", "mug-sentinel"}
    # Protected bodies never appear in the public catalog.
    assert "SYNTHETIC DEMO CONTENT" not in json.dumps(catalog)
    assert client.get("/commerce/offers").json()["offers"] == offers


def test_paid_content_answers_402_with_the_offers_that_unlock_it(client: TestClient) -> None:
    resp = client.get(VIDEO)
    assert resp.status_code == 402
    body = resp.json()
    assert body["offers"] == [
        {"offer_type": "access", "item_id": "agent-economy-video", "title": body["offers"][0]["title"], "price": "2.00", "currency": "USD"}
    ]
    members = client.get("/paid/members/daily-feed").json()
    assert members["offers"][0]["offer_type"] == "subscription"
    assert client.get("/paid/media/unknown").status_code == 404


def test_access_purchase_unlocks_only_the_item_bought(client: TestClient) -> None:
    paid = _buy_video(client)
    assert paid["status"] == "PAID" and paid["fulfillment_status"] == "DELIVERED"
    assert paid["amount"] == "2.00" and paid["simulated"] is True
    token = paid["access_token"]
    assert token
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get(VIDEO, headers=auth).status_code == 200
    assert client.get("/paid/media/data/agent-traffic-q3", headers=auth).status_code == 402
    assert client.get("/paid/members/daily-feed", headers=auth).status_code == 402
    # A second capture is idempotent and never re-issues the token.
    again = client.post(f"/commerce/orders/{paid['order_id']}/capture", json={"agent_id": "agent-alpha"}).json()
    assert again["status"] == "PAID" and again["access_token"] is None
    assert client.get(VIDEO, headers={"Authorization": "Bearer not-a-real-token"}).json()["reason_code"] == "INVALID_TOKEN"


def test_product_cart_is_priced_by_the_site_including_shipping(client: TestClient) -> None:
    order = client.post(
        "/commerce/checkout",
        json={"agent_id": "agent-alpha", "offer_type": "product", "items": [{"sku": "ebook-agent-economy", "quantity": 1}, {"sku": "mug-sentinel", "quantity": 2}]},
    ).json()
    # 12.00 + 2 x 14.00 + 4.99 shipping for the physical line.
    assert order["amount"] == "44.99" and order["shipping"] == "4.99"
    assert [l["category"] for l in order["lines"]] == ["DIGITAL_GOODS", "PHYSICAL_GOODS"]
    paid = client.post(f"/commerce/orders/{order['order_id']}/capture", json={"agent_id": "agent-alpha"}).json()
    assert paid["fulfillment_status"] == "PROCESSING"  # something ships
    assert paid["grants"] == ["/downloads/agent-economy-playbook"]
    assert client.get("/paid/downloads/agent-economy-playbook", headers={"Authorization": f"Bearer {paid['access_token']}"}).status_code == 200


def test_checkout_rejects_client_prices_and_bad_carts(client: TestClient) -> None:
    base = {"agent_id": "agent-alpha", "offer_type": "access", "item_id": "agent-economy-video"}
    assert client.post("/commerce/checkout", json={**base, "price": "0.01"}).status_code == 400
    assert client.post("/commerce/checkout", json={**base, "item_id": "nope"}).status_code == 404
    assert client.post("/commerce/checkout", json={"agent_id": "agent-alpha", "offer_type": "product", "items": []}).status_code == 400
    dup = [{"sku": "mug-sentinel", "quantity": 1}, {"sku": "mug-sentinel", "quantity": 1}]
    assert client.post("/commerce/checkout", json={"agent_id": "agent-alpha", "offer_type": "product", "items": dup}).status_code == 400
    assert client.post("/commerce/checkout", json={"agent_id": "agent-alpha", "offer_type": "product", "items": [{"sku": "mug-sentinel", "quantity": 11}]}).status_code == 400


def test_orders_are_private_to_their_agent(client: TestClient) -> None:
    paid = _buy_video(client, "agent-alpha")
    assert client.get(f"/commerce/orders/{paid['order_id']}", headers={"X-Agent-Id": "agent-beta"}).status_code == 404
    assert client.post(f"/commerce/orders/{paid['order_id']}/capture", json={"agent_id": "agent-beta"}).status_code == 404
    assert client.get(f"/commerce/orders/{paid['order_id']}", headers={"X-Agent-Id": "agent-alpha"}).json()["status"] == "PAID"


def test_unpaid_orders_can_be_cancelled_but_paid_ones_cannot(client: TestClient) -> None:
    order = client.post("/commerce/checkout", json={"agent_id": "agent-alpha", "offer_type": "access", "item_id": "agent-traffic-dataset"}).json()
    cancelled = client.post(f"/commerce/orders/{order['order_id']}/cancel", json={"agent_id": "agent-alpha"}).json()
    assert cancelled["status"] == "CANCELLED"
    paid = _buy_video(client)
    assert client.post(f"/commerce/orders/{paid['order_id']}/cancel", json={"agent_id": "agent-alpha"}).status_code == 409


def test_subscription_unlocks_members_pages_until_cancelled(client: TestClient) -> None:
    sub = client.post("/commerce/subscriptions", json={"agent_id": "agent-alpha", "plan_id": "sentinel-pro-monthly", "purpose": "daily feed"})
    assert sub.status_code == 201
    body = sub.json()
    assert body["status"] == "ACTIVE" and body["amount"] == "9.99" and body["access_token"]
    auth = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/paid/members/daily-feed", headers=auth).status_code == 200
    assert client.get("/paid/members/weekly-deep-dive", headers=auth).status_code == 200
    assert client.get(VIDEO, headers=auth).status_code == 402
    assert client.post("/commerce/subscriptions", json={"agent_id": "agent-alpha", "plan_id": "sentinel-pro-monthly"}).status_code == 409
    cancelled = client.post(f"/commerce/subscriptions/{body['subscription_ref']}/cancel", json={"agent_id": "agent-alpha"}).json()
    assert cancelled["status"] == "CANCELLED"
    after = client.get("/paid/members/daily-feed", headers=auth)
    assert after.status_code == 402 and after.json()["reason_code"] == "SUBSCRIPTION_NOT_ACTIVE"


def test_summary_reports_revenue_by_payment_type(client: TestClient) -> None:
    _buy_video(client)
    client.post("/commerce/subscriptions", json={"agent_id": "agent-alpha", "plan_id": "sentinel-pro-monthly"})
    summary = client.get("/commerce/summary").json()
    assert summary["access_revenue"] == "2.00"
    assert summary["subscription_mrr"] == "9.99"
    assert summary["active_subscriptions"] == 1
    assert all("access_token_hash" not in o for o in summary["orders"])
    assert summary["deposits"]["total"] == 0


def test_commerce_state_survives_a_restart(test_settings: Settings) -> None:
    with TestClient(create_app(settings=test_settings)) as first:
        token = _buy_video(first)["access_token"]
    with TestClient(create_app(settings=test_settings)) as second:
        assert second.get(VIDEO, headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_commerce_summary_requires_dashboard_access(test_settings: Settings) -> None:
    with TestClient(create_app(settings=test_settings)) as c:
        assert c.get("/commerce/summary").status_code == 401
        assert c.get("/commerce/summary", headers={"X-Admin-Token": "test-admin-secret-token-999"}).status_code == 200


# --- PayPal Sandbox request bodies -----------------------------------------------------------


def _sandbox(test_settings: Settings, requests: list[httpx.Request], responses: dict[str, Any]) -> PayPalSandboxProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        for prefix, reply in responses.items():
            if request.url.path.startswith(prefix):
                status, body = reply
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    settings = test_settings.model_copy(update={"payment_mode": "paypal_sandbox"})
    return PayPalSandboxProvider(settings, transport=httpx.MockTransport(handler))


async def test_itemized_order_sends_items_and_breakdown(test_settings: Settings) -> None:
    requests: list[httpx.Request] = []
    provider = _sandbox(test_settings, requests, {"/v2/checkout/orders": (201, {"id": "ORDER1", "status": "CREATED", "links": [{"rel": "payer-action", "href": "https://paypal.test/approve"}]})})
    result = await provider.create_order(
        deposit_id="ord_1",
        amount=Decimal("44.99"),
        currency="USD",
        request_id="r1",
        description="Sentinel News: 2 items",
        items=[
            OrderLineItem(name="E-book", unit_amount=Decimal("12.00"), sku="ebook"),
            OrderLineItem(name="Mug", unit_amount=Decimal("14.00"), quantity=2, sku="mug", category="PHYSICAL_GOODS"),
        ],
        shipping=Decimal("4.99"),
        callback_path="/commerce/paypal",
    )
    assert result.approval_url == "https://paypal.test/approve"
    body = json.loads(requests[-1].content)
    unit = body["purchase_units"][0]
    assert unit["amount"] == {
        "currency_code": "USD",
        "value": "44.99",
        "breakdown": {"item_total": {"currency_code": "USD", "value": "40.00"}, "shipping": {"currency_code": "USD", "value": "4.99"}},
    }
    assert [(i["sku"], i["quantity"], i["category"]) for i in unit["items"]] == [("ebook", "1", "DIGITAL_GOODS"), ("mug", "2", "PHYSICAL_GOODS")]
    context = body["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"].endswith("/commerce/paypal/return?order_id=ord_1")
    assert "shipping_preference" not in context  # physical goods collect an address
    with pytest.raises(PaymentProviderError):
        await provider.create_order(deposit_id="ord_2", amount=Decimal("1.00"), currency="USD", request_id="r2", description="x", items=[OrderLineItem(name="E-book", unit_amount=Decimal("12.00"))])


async def test_subscription_calls_paypal_subscriptions_api(test_settings: Settings) -> None:
    requests: list[httpx.Request] = []
    provider = _sandbox(
        test_settings,
        requests,
        {
            "/v1/catalogs/products": (201, {"id": "PROD-1"}),
            "/v1/billing/plans": (201, {"id": "P-PLAN", "status": "ACTIVE"}),
            "/v1/billing/subscriptions/I-SUB/cancel": (204, {}),
            "/v1/billing/subscriptions/I-SUB": (200, {"id": "I-SUB", "status": "ACTIVE", "plan_id": "P-PLAN", "billing_info": {"next_billing_time": "2026-12-07T10:00:00Z", "last_payment": {"amount": {"value": "9.99", "currency_code": "USD"}}}}),
            "/v1/billing/subscriptions": (201, {"id": "I-SUB", "status": "APPROVAL_PENDING", "links": [{"rel": "approve", "href": "https://paypal.test/sub"}]}),
        },
    )
    plan_id = await provider.create_subscription_plan(name="Pro", description="d", price=Decimal("9.99"), currency="USD", interval_unit="MONTH", interval_count=1, request_id="p1")
    assert plan_id == "P-PLAN"
    plan_body = json.loads(next(r for r in requests if r.url.path == "/v1/billing/plans").content)
    assert plan_body["billing_cycles"][0]["pricing_scheme"]["fixed_price"] == {"value": "9.99", "currency_code": "USD"}
    created = await provider.create_subscription(plan_id=plan_id, custom_id="sub_1", request_id="s1")
    assert created.status == "APPROVAL_PENDING" and created.approval_url == "https://paypal.test/sub"
    sub_body = json.loads(next(r for r in requests if r.url.path == "/v1/billing/subscriptions").content)
    assert sub_body["plan_id"] == "P-PLAN" and sub_body["custom_id"] == "sub_1"
    assert sub_body["application_context"]["return_url"].endswith("/commerce/paypal/return?subscription_ref=sub_1")
    current = await provider.get_subscription(subscription_id="I-SUB")
    assert current.status == "ACTIVE" and current.last_payment_amount == Decimal("9.99")
    await provider.cancel_subscription(subscription_id="I-SUB", reason="done")
    assert any(r.url.path == "/v1/billing/subscriptions/I-SUB/cancel" for r in requests)
    assert all(r.headers.get("Authorization") == "Bearer tok" for r in requests if r.url.path != "/v1/oauth2/token")


async def test_human_checkout_never_uses_the_sandbox_test_card(test_settings: Settings) -> None:
    requests: list[httpx.Request] = []
    settings = test_settings.model_copy(update={"payment_mode": "paypal_sandbox", "paypal_sandbox_autopay": True})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(201, json={"id": f"ORDER{len(requests)}", "status": "CREATED"})

    provider = PayPalSandboxProvider(settings, transport=httpx.MockTransport(handler))
    item = [OrderLineItem(name="Video", unit_amount=Decimal("2.00"), sku="v")]
    await provider.create_order(deposit_id="ord_a", amount=Decimal("2.00"), currency="USD", request_id="a", description="x", items=item)
    await provider.create_order(deposit_id="ord_h", amount=Decimal("2.00"), currency="USD", request_id="h", description="x", items=item, autopay=False)
    agent_body, human_body = (json.loads(r.content) for r in requests if r.url.path == "/v2/checkout/orders")
    assert "card" in agent_body["payment_source"]
    assert "paypal" in human_body["payment_source"] and "card" not in human_body["payment_source"]


def test_js_sdk_client_id_is_only_exposed_in_sandbox_mode(client: TestClient) -> None:
    assert client.get("/demo/config").json()["paypal_js_client_id"] is None
