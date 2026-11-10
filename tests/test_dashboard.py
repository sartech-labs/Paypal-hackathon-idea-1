"""Tests for the publisher dashboard page and its data endpoints."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow
from tests.fakes import FakeTextGenerator

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}


def _client(settings: Settings, clock: FakeClock, **kwargs: Any) -> httpx.AsyncClient:
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock), **kwargs)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _one_refunded_one_forfeited(client: httpx.AsyncClient) -> tuple[str, str]:
    _, good_deposit, good = await complete_onboarding_flow(client, "crawler-good")
    good_headers = {"Authorization": f"Bearer {good['access_token']}"}
    assert (await client.get("/gateway/news", headers=good_headers)).status_code == 200
    assert (await client.post("/sessions/complete", headers=good_headers)).status_code == 200

    _, bad_deposit, bad = await complete_onboarding_flow(client, "crawler-bad")
    bad_headers = {"Authorization": f"Bearer {bad['access_token']}"}
    assert (await client.get("/gateway/admin", headers=bad_headers)).status_code == 403
    assert (await client.post("/sessions/complete", headers=bad_headers)).status_code == 200
    return good_deposit["deposit_id"], bad_deposit["deposit_id"]


@pytest.mark.asyncio
async def test_dashboard_page_and_assets_are_served(async_client: httpx.AsyncClient) -> None:
    page = await async_client.get("/console")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "SARA publisher console" in page.text
    assert "ag-grid-community@36.2.0" in page.text
    assert 'integrity="sha384-' in page.text
    assert (await async_client.get("/dashboard")).status_code == 200
    for asset in ("/_sara/static/dashboard.js", "/_sara/static/dashboard.css", "/_sara/static/grid_kit.js", "/_sara/static/analytics.js"):
        assert (await async_client.get(asset)).status_code == 200


@pytest.mark.asyncio
async def test_console_loads_shared_grid_kit_and_pinned_ag_studio(async_client: httpx.AsyncClient) -> None:
    page = (await async_client.get("/console")).text
    for script in ("/_sara/static/grid_kit.js", "/_sara/static/analytics.js", "/_sara/static/dashboard.js"):
        assert script in page
    for view in ("overview", "operations", "security", "analytics", "policy"):
        assert f'data-view="{view}"' in page
    # AG Studio is fetched on demand from a pinned version with a Subresource Integrity hash.
    analytics = (await async_client.get("/_sara/static/analytics.js")).text
    assert "ag-studio@3.1.0/dist/umd/ag-studio.min.js" in analytics
    assert 'STUDIO_SRI = "sha384-' in analytics
    # The live demo uses the same grid kit for its request monitor.
    demo = (await async_client.get("/crawler")).text
    assert "/_sara/static/grid_kit.js" in demo and 'id="grid-requests"' in demo
    # Empty state, live toasts, and the admin-token popover (token form IDs are unchanged).
    for marker in ('id="console-empty"', 'id="toasts"', 'id="admin-toggle"', 'id="token-form"', 'id="admin-token"'):
        assert marker in page
    for marker in ('id="admin-toggle"', 'id="token-form"', 'id="admin-token"', 'aria-keyshortcuts="S"'):
        assert marker in demo
    assert "prefetch" in analytics


@pytest.mark.asyncio
async def test_dashboard_data_requires_admin_unless_public(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(test_settings, fake_clock) as client:
        assert (await client.get("/dashboard/data")).status_code == 401
        admin = await client.get("/dashboard/data", headers=ADMIN)
        assert admin.status_code == 200 and admin.json()["access"] == "admin"

    public_settings = test_settings.model_copy(update={"dashboard_public": True})
    async with _client(public_settings, fake_clock) as client:
        public = await client.get("/dashboard/data")
        assert public.status_code == 200 and public.json()["access"] == "public"
        # An explicit wrong token is still rejected rather than silently downgraded.
        wrong = await client.get("/dashboard/data", headers={"X-Admin-Token": "wrong"})
        assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_kpis_and_rows(async_client: httpx.AsyncClient) -> None:
    good_id, bad_id = await _one_refunded_one_forfeited(async_client)
    data = (await async_client.get("/dashboard/data", headers=ADMIN)).json()

    kpis = data["kpis"]
    assert kpis["sessions_total"] == 2 and kpis["sessions_active"] == 0
    assert kpis["deposits_total"] == 2
    assert kpis["deposits_returned"] == 1 and kpis["amount_returned"] == "5.00"
    assert kpis["deposits_forfeited"] == 1 and kpis["amount_forfeited"] == "5.00"
    assert kpis["deposits_held"] == 0 and kpis["deposits_needing_attention"] == 0
    assert kpis["violations_by_reason"] == {"DISALLOWED_PATH": 1}
    assert kpis["gateway_requests_allowed"] == 1 and kpis["gateway_requests_rejected"] == 1

    states = {row["deposit_id"]: row["deposit_state"] for row in data["deposits"]}
    assert states == {good_id: "REFUNDED", bad_id: "FORFEITED"}
    assert data["settlement_mode"] == "refund" and data["payment_mode"] == "mock"

    assert data["events"], "expected audit events"
    assert all("details" not in event for event in data["events"])
    timestamps = [event["timestamp"] for event in data["events"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_dashboard_counts_held_deposits(async_client: httpx.AsyncClient) -> None:
    await complete_onboarding_flow(async_client, "crawler-live")
    kpis = (await async_client.get("/dashboard/data", headers=ADMIN)).json()["kpis"]
    assert kpis["sessions_active"] == 1
    assert kpis["deposits_held"] == 1


@pytest.mark.asyncio
async def test_dashboard_explanation_cost_guard(test_settings: Settings, fake_clock: FakeClock) -> None:
    generator = FakeTextGenerator()
    public_settings = test_settings.model_copy(update={"dashboard_public": True})
    async with _client(public_settings, fake_clock, ai_generator=generator) as client:
        _, live_deposit, _ = await complete_onboarding_flow(client, "crawler-live-public")
        live = await client.get(f"/dashboard/deposits/{live_deposit['deposit_id']}/explanation")
        assert live.status_code == 200
        assert live.json()["generated_by"] == "template"
        assert generator.calls == []

        _, bad_id = await _one_refunded_one_forfeited(client)
        generator.responses = [
            {"headline": "Kept", "summary": "Forbidden path.", "findings": [{"statement": "Requested /admin.", "evidence": ["E1"]}]}
        ]
        final = (await client.get(f"/dashboard/deposits/{bad_id}/explanation")).json()
        assert final["generated_by"] == "claude"
        assert len(generator.calls) == 1

        missing = await client.get("/dashboard/deposits/dep_missing/explanation")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_explanation_requires_access(async_client: httpx.AsyncClient) -> None:
    _, deposit, _ = await complete_onboarding_flow(async_client, "crawler-private")
    url = f"/dashboard/deposits/{deposit['deposit_id']}/explanation"
    assert (await async_client.get(url)).status_code == 401
    assert (await async_client.get(url, headers=ADMIN)).status_code == 200
