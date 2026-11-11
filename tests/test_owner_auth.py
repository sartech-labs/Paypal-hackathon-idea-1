"""Owner sign-in: the home page, the console gate, and cookie-based owner rights."""

from __future__ import annotations

import httpx
import pytest

from server.config import Settings
from server.core.owner_auth import COOKIE_NAME, OwnerAuth
from server.main import create_app
from server.services.origin_proxy import OriginProxy
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock

OWNER = {"email": "owner@example.test", "password": "correct horse battery"}
BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
}


def _client(settings: Settings, clock: FakeClock) -> httpx.AsyncClient:
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=BROWSER_UA
    )


@pytest.fixture
def owner_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={"owner_email": OWNER["email"], "owner_password": OWNER["password"]}
    )


@pytest.mark.asyncio
async def test_console_is_open_when_sign_in_is_not_configured(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(test_settings, fake_clock) as client:
        me = (await client.get("/auth/me")).json()
        assert me["login_enabled"] is False and me["signed_in"] is True
        assert (await client.get("/console")).status_code == 200


@pytest.mark.asyncio
async def test_signed_out_visitors_get_the_home_page(
    owner_settings: Settings, fake_clock: FakeClock
) -> None:
    async with _client(owner_settings, fake_clock) as client:
        home = await client.get("/")
        assert home.status_code == 200 and "Good agents get in" in home.text
        console = await client.get("/console")
        assert console.status_code == 303 and console.headers["location"] == "/signin"
        assert "Good agents get in" in (await client.get("/signin")).text
        # Without a session, owner-only APIs stay closed.
        assert (await client.get("/admin/sites")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_wrong_password_is_rejected(owner_settings: Settings, fake_clock: FakeClock) -> None:
    async with _client(owner_settings, fake_clock) as client:
        bad = await client.post("/auth/login", json={**OWNER, "password": "nope"})
        assert bad.status_code == 401
        assert COOKIE_NAME not in bad.cookies
        assert (await client.get("/console")).status_code == 303


@pytest.mark.asyncio
async def test_owner_session_opens_the_console_and_admin_api(
    owner_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    monkeypatch.setattr(OriginProxy, "import_robots", lambda self: self.rules)
    async with _client(owner_settings, fake_clock) as client:
        login = await client.post("/auth/login", json={**OWNER, "email": "Owner@Example.test"})
        assert login.status_code == 200, login.text
        assert COOKIE_NAME in login.cookies

        me = (await client.get("/auth/me")).json()
        assert me["signed_in"] is True and me["email"] == OWNER["email"]
        assert (await client.get("/console")).status_code == 200
        assert "SARA console" in (await client.get("/")).text
        assert (await client.get("/signin")).status_code == 303

        # The cookie carries owner rights: same-origin writes work without X-Admin-Token.
        listed = await client.get("/admin/sites")
        assert listed.status_code == 200, listed.text
        created = await client.post(
            "/admin/sites",
            json={"origin_url": "https://owner-site.example"},
            headers={"Origin": "http://testserver"},
        )
        assert created.status_code == 201, created.text

        logout = await client.post("/auth/logout")
        assert logout.status_code == 200
        client.cookies.clear()
        assert (await client.get("/console")).status_code == 303


@pytest.mark.asyncio
async def test_cross_site_writes_do_not_ride_the_owner_cookie(
    owner_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    monkeypatch.setattr(OriginProxy, "import_robots", lambda self: self.rules)
    async with _client(owner_settings, fake_clock) as client:
        assert (await client.post("/auth/login", json=OWNER)).status_code == 200
        forged = await client.post(
            "/admin/sites",
            json={"origin_url": "https://attacker-chosen.example"},
            headers={"Origin": "https://evil.example"},
        )
        assert forged.status_code in (401, 403)


@pytest.mark.asyncio
async def test_demo_sign_in_only_when_enabled(test_settings: Settings, fake_clock: FakeClock) -> None:
    async with _client(test_settings, fake_clock) as client:
        assert (await client.post("/auth/demo")).status_code == 404

    demo = test_settings.model_copy(update={"demo_signin": True})
    async with _client(demo, fake_clock) as client:
        assert (await client.get("/console")).status_code == 303
        signed = await client.post("/auth/demo")
        assert signed.status_code == 200 and signed.json()["demo"] is True
        me = (await client.get("/auth/me")).json()
        assert me["signed_in"] is True and me["demo"] is True
        assert me["password_login"] is False
        assert (await client.get("/console")).status_code == 200


@pytest.mark.asyncio
async def test_sign_in_is_rate_limited(owner_settings: Settings, fake_clock: FakeClock) -> None:
    async with _client(owner_settings, fake_clock) as client:
        for _ in range(10):
            await client.post("/auth/login", json={**OWNER, "password": "wrong"})
        blocked = await client.post("/auth/login", json=OWNER)
        assert blocked.status_code == 429


def test_tampered_or_expired_cookies_are_rejected(owner_settings: Settings) -> None:
    auth = OwnerAuth(owner_settings)
    cookie = auth.issue(OWNER["email"], now=1000.0)
    assert auth.verify(cookie, now=1001.0)["email"] == OWNER["email"]
    body, _, signature = cookie.rpartition(".")
    assert auth.verify(f"{body}x.{signature}", now=1001.0) is None
    assert auth.verify(cookie, now=1000.0 + 13 * 3600) is None
    assert OwnerAuth(owner_settings).verify(cookie, now=1001.0) is None  # different secret
