"""Integration tests for the FastAPI SARA gateway, path normalization, and auth."""

from __future__ import annotations

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow


@pytest.mark.asyncio
async def test_public_endpoints_and_402_challenge(async_client: httpx.AsyncClient) -> None:
    health = await async_client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    policies = await async_client.get("/policies")
    assert policies.status_code == 200
    assert policies.json()["site_id"] == "default"

    robots = await async_client.get("/robots.txt")
    assert robots.status_code == 200
    assert "User-agent: *" in robots.text
    assert "Disallow: /admin" in robots.text

    # Unauthenticated gateway request returns 402 Payment Required challenge
    chg_resp = await async_client.get("/gateway/news", headers={"X-Crawler-Id": "bot-alpha"})
    assert chg_resp.status_code == 402
    data = chg_resp.json()
    assert data["error"] == "payment_required"
    assert data["crawler_id"] == "bot-alpha"
    assert data["deposit"]["amount"] == "5.00"  # /news is Public-tier content
    assert data["deposit"]["currency"] == "USD"
    assert data["policy_url"] == "/policies"
    assert data["payment_url"] == "/deposits/create"


@pytest.mark.asyncio
async def test_gateway_allowed_disallowed_unknown_and_traversal_paths(
    async_client: httpx.AsyncClient,
    fake_clock: FakeClock,
) -> None:
    _, _, session_issue = await complete_onboarding_flow(async_client, "crawler-paths")
    headers = {"Authorization": f"Bearer {session_issue['access_token']}"}

    # Allowed paths return 200 HTML
    r_root = await async_client.get("/gateway", headers=headers)
    assert r_root.status_code == 200
    assert "Sentinel News" in r_root.text

    r_news = await async_client.get("/gateway/news", headers=headers)
    assert r_news.status_code == 200
    assert "Top Headlines" in r_news.text

    # Repeated slashes on allowed path normalize cleanly
    r_norm = await async_client.get("/gateway//news///technology", headers=headers)
    assert r_norm.status_code == 200
    assert "Technology Desk" in r_norm.text

    # Advance clock so rate limiter window is fresh
    fake_clock.advance(15.0)

    # Unknown path returns 404 without disqualifying the session
    r_404 = await async_client.get("/gateway/nonexistent-section", headers=headers)
    assert r_404.status_code == 404
    assert r_404.json()["reason_code"] == "UNKNOWN_PATH"
    assert r_404.json()["violation"] is False

    # Disallowed paths return 403 Forbidden and never leak synthetic restricted content
    for disallowed_target in (
        "/gateway/admin",
        "/gateway/private/customer-data",
        "/gateway/internal/reports",
        "/gateway///admin//",
    ):
        resp = await async_client.get(disallowed_target, headers=headers)
        assert resp.status_code == 403
        body = resp.json()
        assert body["reason_code"] == "DISALLOWED_PATH"
        assert body["violation"] is True
        assert body["disqualifying"] is True
        assert "SYNTHETIC DEMO CONTENT" not in resp.text

    # URL-encoded traversal, double encoding, encoded slashes, and backslashes return 403 INVALID_PATH
    traversal_vectors = [
        "/gateway/%2e%2e/admin",
        "/gateway/news/%2e%2e/admin",
        "/gateway/%252e%252e/admin",
        "/gateway/news%2f..%2fadmin",
        "/gateway/%5cadmin",
    ]
    for vector in traversal_vectors:
        resp = await async_client.get(vector, headers=headers)
        assert resp.status_code == 403, f"Vector {vector} did not return 403"
        body = resp.json()
        assert body["reason_code"] == "INVALID_PATH"
        assert body["violation"] is True


@pytest.mark.asyncio
async def test_gateway_rate_limiting_and_session_states(
    test_settings: Settings,
    fake_clock: FakeClock,
) -> None:
    test_settings.rate_limit_requests = 2
    test_settings.rate_limit_window_seconds = 10.0
    app = create_app(
        settings=test_settings,
        rate_limiter=SlidingWindowRateLimiter(clock=fake_clock),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Invalid token is rejected with 401
        bad_auth = await client.get(
            "/gateway/news",
            headers={"Authorization": "Bearer invalid_token_value"},
        )
        assert bad_auth.status_code == 401

        _, _, session_issue = await complete_onboarding_flow(client, "crawler-rate")
        headers = {"Authorization": f"Bearer {session_issue['access_token']}"}

        # 2 requests succeed within window
        assert (await client.get("/gateway/news", headers=headers)).status_code == 200
        assert (await client.get("/gateway/news/economy", headers=headers)).status_code == 200

        # 3rd request in same window returns 429
        r3 = await client.get("/gateway/news/technology", headers=headers)
        assert r3.status_code == 429
        assert r3.json()["reason_code"] == "RATE_LIMIT_EXCEEDED"
        assert "Retry-After" in r3.headers

        # Advance clock past 10s window -> request succeeds again
        fake_clock.advance(11.0)
        assert (await client.get("/gateway/news", headers=headers)).status_code == 200

        # Revoke session -> subsequent gateway calls return 401 SESSION_REVOKED
        rev = await client.post("/sessions/revoke", headers=headers)
        assert rev.status_code == 200
        assert rev.json()["status"] == "REVOKED"

        after_rev = await client.get("/gateway/news", headers=headers)
        assert after_rev.status_code == 401
        assert after_rev.json()["reason_code"] == "SESSION_REVOKED"

