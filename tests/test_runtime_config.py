"""Live owner configuration: target URL, tier deposits, and crawling rules."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.origin_proxy import OriginProxy
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}


def _client(settings: Settings, clock: FakeClock) -> httpx.AsyncClient:
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_config_requires_admin_unless_demo_public(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(test_settings, fake_clock) as client:
        assert (await client.get("/admin/config")).status_code == 401
        assert (await client.get("/admin/config", headers=ADMIN)).status_code == 200
        public_settings = test_settings.model_copy(update={"demo_config_public": True})
    async with _client(public_settings, fake_clock) as client:
        response = await client.get("/admin/config")
        assert response.status_code == 200 and response.json()["config_public"] is True


async def test_live_update_changes_prices_rules_and_challenge(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(test_settings, fake_clock) as client:
        before = (await client.get("/admin/config", headers=ADMIN)).json()
        assert before["origin_url"] == ""
        response = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={
                "deposit_amount": "2.50",
                "tier_prices": {"premium": "30.00", "standard": "12.00", "public": "6.00"},
                "allowed_paths": ["/", "/news", "/news/technology"],
                "disallowed_paths": ["/admin", "/secret"],
                "max_requests_per_session": 4,
                "rate_limit_requests": 2,
                "rate_limit_window_seconds": 60,
            },
        )
        assert response.status_code == 200, response.text
        after = response.json()
        assert after["deposit_amount"] == "2.50"
        assert after["max_requests_per_session"] == 4
        assert after["policy_version"] != before["policy_version"]
        assert [tier["price"] for tier in after["pricing_tiers"]] == ["30.00", "12.00", "6.00"]

        policy = (await client.get("/policies")).json()
        assert policy["max_requests_per_session"] == 4
        assert policy["disallowed_paths"] == ["/admin", "/secret"]

        challenge = await client.get("/gateway/news", headers={"X-Crawler-Id": "live-config"})
        assert challenge.status_code == 402
        assert challenge.json()["deposit"]["amount"] == "6.00"

        saved = (await client.get("/admin/config", headers=ADMIN)).json()
        assert saved["version"] == after["version"]
        assert saved["updated_by"] == "admin"


async def test_public_config_rejects_private_origin(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    public_settings = test_settings.model_copy(update={"demo_config_public": True})
    async with _client(public_settings, fake_clock) as client:
        response = await client.put(
            "/admin/config",
            headers={"Content-Type": "application/json"},
            json={"origin_url": "http://127.0.0.1:9000"},
        )
        # With no allowlist, every public origin change is rejected before DNS resolution.
        assert response.status_code == 403
        assert "DEMO_ORIGIN_ALLOWLIST" in response.json()["message"]

    async with _client(test_settings, fake_clock) as client:
        # The admin token may point at an internal origin (the normal Docker/compose setup).
        response = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"origin_url": "http://origin:8080"},
        )
        assert response.status_code == 200, response.text


async def test_public_origin_change_requires_allowlist(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    monkeypatch.setattr(OriginProxy, "import_robots", lambda self: self.rules)
    public_settings = test_settings.model_copy(update={"demo_config_public": True})
    async with _client(public_settings, fake_clock) as client:
        denied = await client.put(
            "/admin/config",
            headers={"Content-Type": "application/json"},
            json={"origin_url": "https://origin.example"},
        )
        assert denied.status_code == 403
        message = denied.json()["message"]
        assert "DEMO_ORIGIN_ALLOWLIST" in message and "admin token" in message.lower()

        # Deposit-only edits do not need an allowlist or a token.
        deposit = await client.put(
            "/admin/config",
            headers={"Content-Type": "application/json"},
            json={"deposit_amount": "2.00"},
        )
        assert deposit.status_code == 200

    allowlisted = test_settings.model_copy(
        update={"demo_config_public": True, "demo_origin_allowlist": "origin.example"}
    )
    async with _client(allowlisted, fake_clock) as client:
        allowed = await client.put(
            "/admin/config",
            headers={"Content-Type": "application/json"},
            json={"origin_url": "https://origin.example"},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["origin_url"] == "https://origin.example"


async def test_env_origin_survives_noop_and_deposit_updates(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    monkeypatch.setattr(OriginProxy, "import_robots", lambda self: self.rules)
    settings = test_settings.model_copy(update={"origin_url": "https://origin.example"})
    async with _client(settings, fake_clock) as client:
        synced = await client.post("/admin/config/sync-robots", headers=ADMIN)
        assert synced.status_code == 200, synced.text
        assert synced.json()["origin_url"] == "https://origin.example"
        assert synced.json()["origin_configured"] is True

        updated = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"deposit_amount": "2.00"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["origin_url"] == "https://origin.example"
        assert updated.json()["origin_configured"] is True

    persisted = settings.gateway_config_path.read_text(encoding="utf-8")
    assert "origin_url" not in persisted


async def test_tier_price_validation(test_settings: Settings, fake_clock: FakeClock) -> None:
    async with _client(test_settings, fake_clock) as client:
        response = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"tier_prices": {"nope": "5.00"}},
        )
        assert response.status_code == 422
        response = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"deposit_amount": "-1"},
        )
        assert response.status_code == 422

async def test_public_config_round_trip_with_env_origin_still_allows_deposit(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    monkeypatch.setattr(OriginProxy, "import_robots", lambda self: self.rules)
    public_settings = test_settings.model_copy(
        update={
            "demo_config_public": True,
            "origin_url": "https://origin.example",
            "rate_limit_window_seconds": 60.0,
        }
    )
    async with _client(public_settings, fake_clock) as client:
        current = (await client.get("/admin/config")).json()
        payload = {
            key: current[key]
            for key in (
                "origin_url",
                "allowed_paths",
                "disallowed_paths",
                "deposit_amount",
                "max_requests_per_session",
                "rate_limit_requests",
                "rate_limit_window_seconds",
            )
        }
        payload["deposit_amount"] = "2.00"
        response = await client.put(
            "/admin/config",
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        assert response.status_code == 200, response.text
        assert response.json()["origin_url"] == "https://origin.example"


async def test_demo_mode_caps_rate_limit_at_ten_per_minute(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    assert test_settings.sara_mode == "demo"
    async with _client(test_settings, fake_clock) as client:
        # The console maximum is accepted; the cap applies to the admin token too.
        accepted = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"rate_limit_requests": 10, "rate_limit_window_seconds": 60},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["rate_limit_requests"] == 10

        # A direct API call cannot bypass the console cap.
        too_many = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"rate_limit_requests": 11, "rate_limit_window_seconds": 60},
        )
        assert too_many.status_code == 422
        assert "10 requests per minute" in too_many.json()["message"]

        # A short window can exceed 10/minute even with a small request count.
        too_fast = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"rate_limit_requests": 2, "rate_limit_window_seconds": 10},
        )
        assert too_fast.status_code == 422
        assert "10 requests per minute" in too_fast.json()["message"]

