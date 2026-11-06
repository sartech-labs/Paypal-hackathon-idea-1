"""Tier-based content pricing: Premium $5, Standard $3, Public $1.

Covers the central tier configuration, server-side deposit pricing in the 402 challenge,
tier enforcement in the policy engine, the live demo's tier selection and request
history, and the publisher console's deposit rows.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.schemas import SiteConfig
from server.services.pricing import PricingCatalog, PricingConfigError
from tests.conftest import FakeClock
from tests.test_demo import ADMIN, _make_client, _results, _wait_for

TIERS = {
    "premium": ("5.00", "/content/premium/market-intelligence-report"),
    "standard": ("3.00", "/content/standard/industry-analysis"),
    "public": ("1.00", "/content/public/general-news"),
}


@pytest.fixture
def demo_client(test_settings: Settings, fake_clock: FakeClock) -> Iterator[TestClient]:
    yield from _make_client(test_settings, fake_clock)


async def _session_for(client: httpx.AsyncClient, path: str, crawler_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Run the 402 -> deposit -> capture -> verify -> accept flow for `path`."""
    challenge = (await client.get(f"/gateway{path}", headers={"X-Crawler-Id": crawler_id})).json()
    ids = {"challenge_id": challenge["challenge_id"], "crawler_id": crawler_id}
    deposit = (await client.post("/deposits/create", json=ids)).json()
    assert (await client.post(f"/deposits/{deposit['deposit_id']}/capture", json=ids)).status_code == 200
    verified = (await client.post(f"/deposits/{deposit['deposit_id']}/verify", json=ids)).json()
    issued = await client.post(
        "/sessions/accept-policy",
        json={**ids, "deposit_id": deposit["deposit_id"], "policy_version": challenge["policy_version"], "agree_to_terms": True},
    )
    assert issued.status_code == 201
    return challenge, verified, issued.json()["access_token"]


# --- configuration ---------------------------------------------------------------------------


async def test_policy_publishes_three_tiers(async_client: httpx.AsyncClient) -> None:
    policy = (await async_client.get("/policies")).json()
    tiers = {tier["tier_id"]: tier for tier in policy["pricing_tiers"]}
    assert {key: tiers[key]["price"] for key in tiers} == {key: price for key, (price, _) in TIERS.items()}
    assert all(tier["currency"] == "USD" for tier in tiers.values())
    for key, (_, path) in TIERS.items():
        assert path in [resource["path"] for resource in tiers[key]["resources"]]
    assert "/content" in policy["allowed_paths"]


def _config(**tier_overrides: Any) -> SiteConfig:
    tier = {
        "tier_id": "gold", "name": "Gold", "description": "d", "price": "2.00", "currency": "USD",
        "rank": 1, "path_prefix": "/gold", "resources": [{"path": "/gold/a", "title": "A", "category": "c"}],
    }
    tier.update(tier_overrides)
    return SiteConfig.model_validate({
        "site_id": "s", "site_name": "S", "policy_version": "1", "deposit_amount": "0.01", "currency": "USD",
        "allowed_paths": ["/"], "disallowed_paths": [], "refund_policy": {"description": "r"}, "pricing_tiers": [tier],
    })


@pytest.mark.parametrize(
    "override",
    [{"price": "0"}, {"price": "abc"}, {"currency": "EUR"}, {"path_prefix": "/"}, {"resources": [{"path": "/other", "title": "x", "category": "c"}]}],
)
def test_invalid_tier_configuration_is_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(PricingConfigError):
        PricingCatalog(_config(**override))


def test_tier_resolution_by_path() -> None:
    catalog = PricingCatalog(_config())
    assert catalog.deposit_for_request_path("/gold/a")[0] == "2.00"
    assert catalog.deposit_for_request_path("/news")[0] == "0.01"
    # Malformed or traversal paths never resolve to a (cheaper or pricier) tier.
    assert catalog.tier_for_request_path("/gold/%2e%2e/a") is None
    assert catalog.tier_for_request_path("/golden") is None


# --- gateway pricing and enforcement ------------------------------------------------------


@pytest.mark.parametrize("tier_id", list(TIERS))
async def test_challenge_and_deposit_use_the_tier_price(async_client: httpx.AsyncClient, tier_id: str) -> None:
    price, path = TIERS[tier_id]
    challenge, deposit, token = await _session_for(async_client, path, f"crawler-{tier_id}")
    assert challenge["deposit"]["amount"] == price
    assert challenge["pricing"] == {
        "tier_id": tier_id, "tier_name": tier_id.capitalize(), "price": price, "currency": "USD", "resource": path,
    }
    assert deposit["required_amount"] == price and deposit["captured_amount"] == price
    assert deposit["pricing_tier"] == tier_id and deposit["requested_resource"] == path

    page = await async_client.get(f"/gateway{path}", headers={"Authorization": f"Bearer {token}"})
    assert page.status_code == 200 and page.headers["X-CrawlerTrust-Pricing-Tier"] == tier_id

    done = (await async_client.post("/sessions/complete", headers={"Authorization": f"Bearer {token}"})).json()
    assert done["deposit"]["deposit_state"] == "REFUNDED"


async def test_news_sections_are_public_tier(async_client: httpx.AsyncClient) -> None:
    for path in ("/news", "/news/economy"):
        challenge = (await async_client.get(f"/gateway{path}", headers={"X-Crawler-Id": "crawler-news"})).json()
        assert challenge["deposit"]["amount"] == "1.00"
        assert challenge["pricing"]["tier_id"] == "public" and challenge["pricing"]["resource"] == path


async def test_untiered_paths_keep_the_base_deposit(async_client: httpx.AsyncClient) -> None:
    challenge = (await async_client.get("/gateway", headers={"X-Crawler-Id": "crawler-base"})).json()
    assert challenge["deposit"]["amount"] == "0.01" and challenge["pricing"] is None


async def test_crawler_cannot_supply_its_own_price(async_client: httpx.AsyncClient) -> None:
    challenge = (await async_client.get("/gateway/content/premium/financial-reports", headers={"X-Crawler-Id": "crawler-cheap"})).json()
    resp = await async_client.post(
        "/deposits/create",
        json={"challenge_id": challenge["challenge_id"], "crawler_id": "crawler-cheap", "amount": "0.01"},
    )
    assert resp.status_code == 400  # extra fields are rejected; the amount is the gateway's


async def test_lower_tier_deposit_does_not_open_higher_tier_content(async_client: httpx.AsyncClient) -> None:
    _, _, token = await _session_for(async_client, TIERS["public"][1], "crawler-upsell")
    auth = {"Authorization": f"Bearer {token}"}
    denied = await async_client.get(f"/gateway{TIERS['premium'][1]}", headers=auth)
    body = denied.json()
    assert denied.status_code == 402 and body["reason_code"] == "TIER_NOT_COVERED"
    assert body["violation"] is False and "Premium" in body["reason"]
    assert "AI Data Licensing" not in denied.text  # the premium page body is never served
    # Same-tier and untiered content stay available; the deposit is still returned.
    assert (await async_client.get(f"/gateway{TIERS['public'][1]}", headers=auth)).status_code == 200
    assert (await async_client.get("/gateway/news", headers=auth)).status_code == 200
    done = (await async_client.post("/sessions/complete", headers=auth)).json()
    assert done["deposit"]["deposit_state"] == "REFUNDED"


async def test_higher_tier_deposit_covers_lower_tiers(async_client: httpx.AsyncClient) -> None:
    _, _, token = await _session_for(async_client, TIERS["premium"][1], "crawler-premium-all")
    auth = {"Authorization": f"Bearer {token}"}
    for path in (TIERS["standard"][1], TIERS["public"][1], "/news"):
        assert (await async_client.get(f"/gateway{path}", headers=auth)).status_code == 200


async def test_base_deposit_does_not_open_tiered_content(async_client: httpx.AsyncClient) -> None:
    _, _, token = await _session_for(async_client, "/", "crawler-base-only")
    for path in (TIERS["public"][1], "/news"):
        resp = await async_client.get(f"/gateway{path}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 402 and resp.json()["reason_code"] == "TIER_NOT_COVERED"


# --- live demo ---------------------------------------------------------------------------------


def test_demo_config_lists_tiers_from_the_backend(demo_client: TestClient) -> None:
    tiers = demo_client.get("/demo/config", headers=ADMIN).json()["pricing_tiers"]
    assert [(t["tier_id"], t["price"]) for t in tiers] == [(k, p) for k, (p, _) in TIERS.items()]


def test_demo_rejects_unknown_tier(demo_client: TestClient) -> None:
    resp = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "compliant", "tier": "platinum"})
    assert resp.status_code == 422


@pytest.mark.parametrize("tier_id", list(TIERS))
def test_scripted_compliant_run_per_tier(demo_client: TestClient, tier_id: str) -> None:
    price, path = TIERS[tier_id]
    started = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "compliant", "tier": tier_id})
    assert started.status_code == 202
    run = _wait_for(demo_client, started.json()["run_id"], ADMIN)

    assert run["status"] == "completed" and run["error"] is None
    assert run["pricing_tier"]["tier_id"] == tier_id and run["pricing_tier"]["price"] == price
    assert run["deposit"]["required_amount"] == price and run["deposit"]["pricing_tier"] == tier_id
    assert run["deposit"]["deposit_state"] == "REFUNDED"
    assert run["session"]["total_violations"] == 0
    pages = _results(run, "fetch_page")
    assert pages[0]["status"] == 402 and pages[0]["deposit"]["amount"] == price
    assert pages[0]["pricing"]["resource"] == path
    assert [p["status"] for p in pages[1:]] == [200, 200, 200]
    assert all(p.get("pricing_tier") == tier_id for p in pages[1:])

    history = demo_client.get(f"/demo/requests?crawler_id={run['crawler_id']}", headers=ADMIN).json()["rows"]
    assert len(history) == 4
    oldest = history[-1]
    assert oldest["http_status"] == 402 and oldest["access_decision"] == "payment_required"
    assert oldest["resource"] == path and oldest["deposit_amount"] == price
    assert oldest["deposit_id"] == run["deposit"]["deposit_id"]
    for row in history[:-1]:
        assert row["http_status"] == 200 and row["access_decision"] == "granted"
        assert row["pricing_tier"] == tier_id and row["deposit_amount"] == price
        assert row["deposit_state"] == "REFUNDED" and row["simulated_payment"] is True

    deposits = demo_client.get("/dashboard/data", headers=ADMIN).json()["deposits"]
    row = next(d for d in deposits if d["deposit_id"] == run["deposit"]["deposit_id"])
    assert row["pricing_tier"] == tier_id and row["amount"] == price and row["requested_resource"] == path
    assert row["provider_capture_id"] and row["provider_refund_id"]


def test_rogue_run_pays_public_tier_for_news_and_forfeits(demo_client: TestClient) -> None:
    started = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "rogue", "tier": "premium"})
    run = _wait_for(demo_client, started.json()["run_id"], ADMIN)
    assert run["pricing_tier"] is None  # the tier selection does not change the rogue script
    # The rogue starts at /news, which is Public content: the gateway charges the Public price.
    assert run["deposit"]["required_amount"] == "1.00" and run["deposit"]["pricing_tier"] == "public"
    assert run["deposit"]["deposit_state"] == "FORFEITED"
    history = demo_client.get(f"/demo/requests?crawler_id={run['crawler_id']}", headers=ADMIN).json()["rows"]
    assert any(r["access_decision"] == "denied" and r["http_status"] == 403 for r in history)


def test_request_history_requires_view_access(demo_client: TestClient) -> None:
    assert demo_client.get("/demo/requests").status_code == 401
    assert demo_client.get("/demo/requests?crawler_id=bad id!", headers=ADMIN).status_code == 422


# --- .env tier price overrides (TIER1_PRICE..TIER3_PRICE) ------------------------------------


def test_env_tier_prices_override_site_config(test_settings: Settings, fake_clock: FakeClock) -> None:
    settings = test_settings.model_copy(update={"tier1_price": Decimal("12.50"), "tier2_price": Decimal("7.00")})
    for client in _make_client(settings, fake_clock):
        tiers = {t["tier_id"]: t["price"] for t in client.get("/demo/config", headers=ADMIN).json()["pricing_tiers"]}
        # tier1 = Premium, tier2 = Standard; tier3 (Public) keeps its site_config price.
        assert tiers == {"premium": "12.50", "standard": "7.00", "public": "1.00"}
        challenge = client.get("/gateway/content/premium/financial-reports", headers={"X-Crawler-Id": "crawler-env"}).json()
        assert challenge["deposit"]["amount"] == "12.50"

        # The demo crawler's spending limit follows the highest configured price. (_make_client
        # swaps in a test runner, so check the container's wiring and mirror it.)
        container = client.app.state.container
        assert create_app(settings=settings).state.container.demo_runner._max_deposit == Decimal("12.50")
        container.demo_runner._max_deposit = container.pricing.max_price()
        started = client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "compliant", "tier": "premium"})
        run = _wait_for(client, started.json()["run_id"], ADMIN)
        assert run["status"] == "completed" and run["deposit"]["required_amount"] == "12.50"
        assert run["deposit"]["deposit_state"] == "REFUNDED"


def test_load_settings_reads_tier_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.config import load_settings_from_env

    monkeypatch.setenv("TIER1_PRICE", "9")
    monkeypatch.setenv("TIER2_PRICE", "")
    monkeypatch.setenv("TIER3_PRICE", "0.5")
    settings = load_settings_from_env()
    assert settings.tier_prices == [Decimal("9.00"), None, Decimal("0.50")]


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_env_tier_price_is_rejected(test_settings: Settings, value: str) -> None:
    with pytest.raises(ValueError):
        Settings(**{**test_settings.model_dump(), "tier1_price": value})
