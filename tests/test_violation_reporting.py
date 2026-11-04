"""Regression tests for how violations are reported in session history and audit logs."""

from __future__ import annotations

import httpx
import pytest

from server.config import Settings
from server.core.security import sanitize_path_for_display
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow


def test_sanitize_path_for_display_escapes_and_truncates() -> None:
    assert sanitize_path_for_display(None) == "/"
    assert sanitize_path_for_display("") == "/"
    assert sanitize_path_for_display("/%2e%2e/admin") == "/%2e%2e/admin"
    assert sanitize_path_for_display("admin") == "/admin"
    assert sanitize_path_for_display("/a\x00b\x7f") == "/a\\x00b\\x7f"
    assert sanitize_path_for_display("/café☃") == "/caf\\xe9\\u2603"

    long_path = "/" + "x" * 500
    shortened = sanitize_path_for_display(long_path)
    assert len(shortened) == 200
    assert shortened.endswith("...")


@pytest.mark.asyncio
async def test_invalid_path_violation_records_the_attempted_path(
    async_client: httpx.AsyncClient,
) -> None:
    _, _, session_issue = await complete_onboarding_flow(async_client, "crawler-trav")
    headers = {"Authorization": f"Bearer {session_issue['access_token']}"}

    resp = await async_client.get("/gateway/%2e%2e/admin", headers=headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason_code"] == "INVALID_PATH"
    assert body["path"] == "/%2e%2e/admin"

    me = (await async_client.get("/sessions/me", headers=headers)).json()
    assert me["violations"][-1]["path"] == "/%2e%2e/admin"
    assert (
        "Disallowed/invalid path access on /%2e%2e/admin (INVALID_PATH)"
        in me["disqualification_reasons"]
    )
    assert not any(" on / (" in reason for reason in me["disqualification_reasons"])

    events = (await async_client.get("/audit/events", headers=headers)).json()
    violation_events = [e for e in events if e["event_type"] == "POLICY_VIOLATION"]
    assert violation_events[-1]["path"] == "/%2e%2e/admin"


@pytest.mark.asyncio
async def test_disallowed_path_violation_keeps_normalized_path(
    async_client: httpx.AsyncClient,
) -> None:
    _, _, session_issue = await complete_onboarding_flow(async_client, "crawler-admin")
    headers = {"Authorization": f"Bearer {session_issue['access_token']}"}

    resp = await async_client.get("/gateway///admin//", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["path"] == "/admin"


@pytest.mark.asyncio
async def test_repeated_rate_limit_violations_add_one_reason(
    test_settings: Settings,
    fake_clock: FakeClock,
) -> None:
    test_settings.rate_limit_requests = 1
    app = create_app(
        settings=test_settings,
        rate_limiter=SlidingWindowRateLimiter(clock=fake_clock),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        _, _, session_issue = await complete_onboarding_flow(client, "crawler-burst")
        headers = {"Authorization": f"Bearer {session_issue['access_token']}"}

        assert (await client.get("/gateway/news", headers=headers)).status_code == 200
        for _ in range(4):
            assert (await client.get("/gateway/news", headers=headers)).status_code == 429

        me = (await client.get("/sessions/me", headers=headers)).json()
        assert me["rate_limit_violations"] == 4
        assert me["disqualified"] is True
        rate_reasons = [r for r in me["disqualification_reasons"] if "Rate limit" in r]
        assert rate_reasons == [
            "Rate limit exceeded repeatedly (disqualification threshold: 2 violations)"
        ]
