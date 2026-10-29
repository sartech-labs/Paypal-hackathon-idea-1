"""Shared pytest fixtures for CrawlerTrust tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter


class FakeClock:
    """Deterministic monotonic clock for rate-limit testing."""

    def __init__(self, initial: float = 1000.0) -> None:
        self._now = initial

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        app_host="127.0.0.1",
        app_port=8000,
        payment_mode="mock",
        paypal_base_url="https://api-m.sandbox.paypal.com",
        paypal_client_id="test_client_id",
        paypal_client_secret="test_client_secret",
        paypal_currency="USD",
        deposit_amount=Decimal("0.01"),
        session_ttl_seconds=1800,
        challenge_ttl_seconds=600,
        max_requests_per_session=10,
        rate_limit_requests=3,
        rate_limit_window_seconds=10.0,
        site_config_path=Path("data/site_config.json"),
        robots_config_path=Path("data/robots.json"),
        allowed_pages_path=Path("data/allowed_pages.json"),
        disallowed_pages_path=Path("data/disallowed_pages.json"),
        audit_log_path=tmp_path / "audit.jsonl",
        payments_db_path=tmp_path / "payments_db.json",
        sessions_db_path=tmp_path / "sessions_db.json",
        admin_api_token="test-admin-secret-token-999",
        log_level="WARNING",
    )


@pytest.fixture
async def async_client(
    test_settings: Settings,
    fake_clock: FakeClock,
) -> AsyncIterator[httpx.AsyncClient]:
    limiter = SlidingWindowRateLimiter(clock=fake_clock)
    app = create_app(settings=test_settings, rate_limiter=limiter)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client


async def complete_onboarding_flow(
    client: httpx.AsyncClient,
    crawler_id: str = "crawler-test-01",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """
    Helper that runs:
    1. GET /gateway/news -> 402 challenge
    2. POST /deposits/create -> 201
    3. POST /deposits/{id}/capture -> 200
    4. POST /deposits/{id}/verify -> 200
    5. POST /sessions/accept-policy -> 201

    Returns (challenge_json, verified_deposit_json, session_issue_json).
    """
    chg_resp = await client.get("/gateway/news", headers={"X-Crawler-Id": crawler_id})
    assert chg_resp.status_code == 402
    challenge = chg_resp.json()
    challenge_id = challenge["challenge_id"]

    dep_resp = await client.post(
        "/deposits/create",
        json={"challenge_id": challenge_id, "crawler_id": crawler_id},
    )
    assert dep_resp.status_code == 201
    deposit = dep_resp.json()
    deposit_id = deposit["deposit_id"]

    cap_resp = await client.post(
        f"/deposits/{deposit_id}/capture",
        json={"crawler_id": crawler_id, "challenge_id": challenge_id},
    )
    assert cap_resp.status_code == 200

    ver_resp = await client.post(
        f"/deposits/{deposit_id}/verify",
        json={"crawler_id": crawler_id, "challenge_id": challenge_id},
    )
    assert ver_resp.status_code == 200
    verified_deposit = ver_resp.json()

    ses_resp = await client.post(
        "/sessions/accept-policy",
        json={
            "challenge_id": challenge_id,
            "deposit_id": deposit_id,
            "crawler_id": crawler_id,
            "policy_version": challenge["policy_version"],
            "agree_to_terms": True,
        },
    )
    assert ses_resp.status_code == 201
    session_issue = ses_resp.json()

    return challenge, verified_deposit, session_issue

