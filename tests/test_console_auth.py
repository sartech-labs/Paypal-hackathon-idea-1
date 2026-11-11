"""Tests for the opt-in owner-console sign-in and its signed session cookie."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from server.config import Settings
from server.core.console_auth import (
    COOKIE_NAME,
    console_auth_enabled,
    resolve_console_credentials,
    sign_console_session,
)
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock

PASSWORD = "test-console-password"
SECRET = "test-console-secret"
ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}
BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _auth_settings(test_settings: Settings) -> Settings:
    """Copy the shared test settings and require console sign-in explicitly."""
    return test_settings.model_copy(
        update={
            "console_auth": "on",
            "console_username": "owner",
            "console_password": PASSWORD,
            "console_session_secret": SECRET,
            # app_env stays "test", so the cookie is not marked Secure over HTTP.
        }
    )


def _client(
    settings: Settings,
    clock: FakeClock,
    *,
    base_url: str = "http://testserver",
    **kwargs: Any,
) -> httpx.AsyncClient:
    app = create_app(
        settings=settings,
        rate_limiter=SlidingWindowRateLimiter(clock=clock),
        **kwargs,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        follow_redirects=False,
    )


async def _login(
    client: httpx.AsyncClient,
    next_path: str = "/console",
    *,
    username: str = "owner",
    password: str = PASSWORD,
) -> httpx.Response:
    return await client.post(
        "/_sara/login",
        data={"username": username, "password": password, "next": next_path},
    )


@pytest.mark.asyncio
async def test_auth_disabled_keeps_console_open_and_sign_in_routes_absent(
    async_client: httpx.AsyncClient,
) -> None:
    """With no console password the console stays open, forged cookies are ignored, and login routes do not exist."""
    for path in ("/console", "/dashboard", "/"):
        assert (await async_client.get(path)).status_code == 200

    session = await async_client.get("/_sara/session")
    assert session.status_code == 200
    assert session.json() == {"authenticated": False, "username": None, "enabled": False}

    async_client.cookies.set(COOKIE_NAME, "forged.value")
    assert (await async_client.get("/console")).status_code == 200
    assert (await async_client.get("/_sara/session")).json() == {
        "authenticated": False,
        "username": None,
        "enabled": False,
    }

    # The sign-in/sign-out routes are not registered, so the site edge answers instead.
    for method in ("get", "post"):
        response = await getattr(async_client, method)("/_sara/login", headers=BROWSER_UA)
        assert response.status_code == 404
    assert (
        await async_client.post("/_sara/logout", headers=BROWSER_UA)
    ).status_code == 404


@pytest.mark.asyncio
async def test_auth_enabled_pages_redirect_to_login_with_quoted_next(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """Without a session the console, dashboard, and demo landing page redirect to sign-in."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        for path in ("/console", "/dashboard", "/"):
            response = await client.get(path)
            assert response.status_code == 303
            assert response.headers["location"] == f"/_sara/login?next={quote(path, safe='')}"

        page = await client.get("/_sara/login")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "SARA sign in" in page.text


@pytest.mark.asyncio
async def test_successful_login_sets_console_cookie_and_redirects_to_next(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """Correct credentials set the HttpOnly SameSite=lax cookie and redirect to the requested same-site path."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        response = await _login(client, "/dashboard")
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"

        set_cookie = response.headers.get("set-cookie", "").lower()
        assert f"{COOKIE_NAME}=" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "max-age=43200" in set_cookie
        assert COOKIE_NAME in client.cookies


@pytest.mark.asyncio
async def test_login_without_next_defaults_to_console(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """A login without a next target redirects to the console."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        response = await client.post(
            "/_sara/login",
            data={"username": "owner", "password": PASSWORD},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/console"


@pytest.mark.asyncio
async def test_wrong_credentials_redirect_with_error_and_set_no_cookie(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """Wrong credentials redirect back to the form with error=1 and do not issue a session."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        response = await _login(client, "/dashboard", password="wrong-password")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/_sara/login?error=1&next=")
        assert "set-cookie" not in response.headers
        assert COOKIE_NAME not in client.cookies


@pytest.mark.asyncio
async def test_console_session_counts_as_admin_for_protected_endpoints(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """A valid console session grants admin access without an X-Admin-Token header."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        assert (await _login(client)).status_code == 303

        assert (await client.get("/console")).status_code == 200
        dashboard = await client.get("/dashboard/data")
        assert dashboard.status_code == 200
        assert dashboard.json()["access"] == "admin"
        assert (await client.get("/admin/config")).status_code == 200
        assert (await client.get("/demo/config")).status_code == 200


@pytest.mark.asyncio
async def test_admin_token_still_grants_access_when_console_auth_is_enabled(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """X-Admin-Token keeps working unchanged while console sign-in is enabled."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        dashboard = await client.get("/dashboard/data", headers=ADMIN)
        assert dashboard.status_code == 200
        assert dashboard.json()["access"] == "admin"
        assert (await client.get("/admin/config", headers=ADMIN)).status_code == 200
        assert (await client.get("/demo/config", headers=ADMIN)).status_code == 200


@pytest.mark.asyncio
async def test_session_endpoint_reports_the_console_auth_state(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """GET /_sara/session reports false/null before login and true/owner afterwards."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        anonymous = await client.get("/_sara/session")
        assert anonymous.status_code == 200
        assert anonymous.json() == {
            "authenticated": False,
            "username": None,
            "enabled": True,
        }

        assert (await _login(client)).status_code == 303
        authenticated = await client.get("/_sara/session")
        assert authenticated.status_code == 200
        assert authenticated.json() == {
            "authenticated": True,
            "username": "owner",
            "enabled": True,
        }


@pytest.mark.asyncio
async def test_tampered_garbage_wrong_secret_and_expired_cookies_are_rejected(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """Malformed, mis-signed, and expired session cookies redirect instead of raising a server error."""
    valid = sign_console_session("owner", SECRET, 300)
    tampered = valid[:-1] + ("A" if valid[-1] != "A" else "B")
    tokens = {
        "tampered": tampered,
        "garbage": "not-a-session",
        "two-parts": "payload.signature",
        "wrong-secret": sign_console_session("owner", "some-other-secret", 300),
        "expired": sign_console_session("owner", SECRET, 60, now=1000.0),
    }

    async with _client(_auth_settings(test_settings), fake_clock) as client:
        for label, token in tokens.items():
            response = await client.get(
                "/console", headers={"cookie": f"{COOKIE_NAME}={token}"}
            )
            assert response.status_code == 303, label
            assert response.headers["location"].startswith("/_sara/login"), label


@pytest.mark.asyncio
async def test_logout_clears_the_cookie_and_returns_to_login(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """POST /_sara/logout deletes the console cookie and redirects to the sign-in page."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        assert (await _login(client)).status_code == 303
        assert COOKIE_NAME in client.cookies

        response = await client.post("/_sara/logout")
        assert response.status_code == 303
        assert response.headers["location"] == "/_sara/login"
        set_cookie = response.headers.get("set-cookie", "").lower()
        assert f"{COOKIE_NAME}=" in set_cookie
        assert "max-age=0" in set_cookie
        assert COOKIE_NAME not in client.cookies

        assert (await client.get("/console")).status_code == 303


@pytest.mark.asyncio
async def test_login_is_rate_limited_per_client_and_recovers_after_the_window(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """Eight login attempts per five minutes are allowed, the next gets 429 with Retry-After, and the window then resets."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        for _ in range(8):
            attempt = await _login(client, password="wrong-password")
            assert attempt.status_code == 303

        limited = await _login(client)
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0

        fake_clock.advance(301)
        recovered = await _login(client)
        assert recovered.status_code == 303
        assert recovered.headers["location"] == "/console"
        assert COOKIE_NAME in client.cookies


@pytest.mark.asyncio
async def test_csrf_guard_rejects_cross_origin_requests_that_carry_the_cookie(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """A console-cookie POST is rejected for a foreign Origin or Sec-Fetch-Site, and allowed same-origin."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        assert (await _login(client)).status_code == 303

        foreign = await client.post(
            "/admin/config", headers={"origin": "https://evil.example"}
        )
        assert foreign.status_code == 403

        cross_site = await client.post(
            "/admin/config", headers={"sec-fetch-site": "cross-site"}
        )
        assert cross_site.status_code == 403

        same_origin_post = await client.post(
            "/admin/config", headers={"origin": "http://testserver"}
        )
        assert same_origin_post.status_code != 403
        same_origin_update = await client.put(
            "/admin/config", headers={"origin": "http://testserver"}, json={}
        )
        assert same_origin_update.status_code == 200


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "//evil.example",
        "https://evil.example",
        "/console\\evil.example",
        "\\\\evil.example",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_next_targets_fall_back_to_console(
    test_settings: Settings, fake_clock: FakeClock, unsafe_next: str
) -> None:
    """Off-site, scheme-qualified, and backslash next targets are replaced with /console."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        response = await _login(client, unsafe_next)
        assert response.status_code == 303
        assert response.headers["location"] == "/console"


def test_console_sign_in_is_required_by_default() -> None:
    """Credentials are required unless the operator explicitly sets CONSOLE_AUTH=off."""
    assert Settings().console_auth == "on"
    assert console_auth_enabled(Settings()) is True
    assert console_auth_enabled(Settings(console_auth="off")) is False


def test_generated_credentials_are_stable_across_restarts_and_env_wins(
    test_settings: Settings,
) -> None:
    """Without CONSOLE_PASSWORD a password is generated, persisted, and reused; env wins."""
    settings = _auth_settings(test_settings).model_copy(
        update={"console_password": "", "console_session_secret": ""}
    )

    first = resolve_console_credentials(settings)
    assert first.generated is True
    assert len(first.password) >= 12
    assert first.username == "owner"

    # A second start (same credential file) agrees with the first one.
    second = resolve_console_credentials(settings)
    assert second.password == first.password
    assert second.session_secret == first.session_secret

    # The stored file is owner-only.
    stored = json.loads(settings.console_credentials_path.read_text())
    assert stored["password"] == first.password
    assert stored["username"] == "owner"
    assert settings.console_credentials_path.stat().st_mode & 0o077 == 0

    # A configured password always wins and is never written to disk.
    configured = settings.model_copy(update={"console_password": "chosen-password"})
    resolved = resolve_console_credentials(configured)
    assert resolved.password == "chosen-password"
    assert resolved.generated is False
    assert "chosen-password" not in settings.console_credentials_path.read_text()


def test_session_secret_is_persisted_when_only_a_password_is_configured(
    test_settings: Settings,
) -> None:
    """A configured password without a secret gets a stored key, so sessions survive restarts."""
    settings = _auth_settings(test_settings).model_copy(
        update={"console_session_secret": ""}
    )

    first = resolve_console_credentials(settings)
    assert first.password == PASSWORD
    assert first.session_secret

    # The key is persisted, so a second worker or a restart verifies the same cookies.
    second = resolve_console_credentials(settings)
    assert second.session_secret == first.session_secret

    # An explicitly configured secret still wins over the stored one.
    configured = settings.model_copy(update={"console_session_secret": SECRET})
    assert resolve_console_credentials(configured).session_secret == SECRET


@pytest.mark.asyncio
async def test_console_password_outranks_public_demo_flags(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """With sign-in enabled, public-demo flags no longer open the dashboard, config, demo, or playground anonymously."""
    settings = _auth_settings(test_settings).model_copy(
        update={
            "dashboard_public": True,
            "demo_public": True,
            "demo_config_public": True,
        }
    )
    protected_paths = (
        "/dashboard/data",
        "/admin/config",
        "/demo/requests",
        "/playground/actions",
    )

    async with _client(settings, fake_clock) as client:
        anonymous_statuses = {
            path: (await client.get(path)).status_code for path in protected_paths
        }
        assert anonymous_statuses == {path: 401 for path in protected_paths}
        assert (await client.put("/admin/config", json={})).status_code == 401
        # /demo/config stays public on purpose: it reports capability metadata only (no
        # deposits, sessions, or runs) and has never required access.
        assert (await client.get("/demo/config")).status_code == 200

        assert (await _login(client)).status_code == 303
        authenticated_statuses = {
            path: (await client.get(path)).status_code for path in protected_paths
        }
        assert authenticated_statuses == {path: 200 for path in protected_paths}


@pytest.mark.asyncio
async def test_csrf_guard_accepts_a_forwarded_public_host(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """An edge proxy's X-Forwarded-Host matching the Origin is trusted, while a foreign Origin is still 403."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        assert (await _login(client)).status_code == 303

        forwarded = await client.put(
            "/admin/config",
            headers={
                "origin": "https://console.example.com",
                "x-forwarded-host": "console.example.com",
            },
            json={},
        )
        assert forwarded.status_code == 200

        foreign = await client.put(
            "/admin/config",
            headers={"origin": "https://evil.example"},
            json={},
        )
        assert foreign.status_code == 403


@pytest.mark.asyncio
async def test_csrf_guard_accepts_the_configured_public_url_host(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """When app_public_url names the public console host, an Origin for that host passes the CSRF guard."""
    settings = _auth_settings(test_settings).model_copy(
        update={"app_public_url": "https://console.example.com"}
    )
    async with _client(settings, fake_clock, base_url="https://testserver") as client:
        assert (await _login(client)).status_code == 303

        response = await client.put(
            "/admin/config",
            headers={"origin": "https://console.example.com"},
            json={},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_successful_sign_ins_do_not_consume_the_login_budget(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """Ten correct sign-ins never 429, and the full eight-failure budget remains before the ninth is throttled."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        for _ in range(10):
            success = await _login(client)
            assert success.status_code == 303
            assert success.headers["location"] == "/console"

        for _ in range(8):
            failure = await _login(client, password="wrong-password")
            assert failure.status_code == 303

        limited = await _login(client, password="wrong-password")
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_valid_signature_for_a_different_username_is_rejected(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """A correctly signed cookie for a username other than the configured one does not grant the console."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        client.cookies.set(COOKIE_NAME, sign_console_session("owner", SECRET, 300))
        assert (await client.get("/console")).status_code == 200

        client.cookies.set(
            COOKIE_NAME, sign_console_session("someone-else", SECRET, 300)
        )
        response = await client.get("/console")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/_sara/login")


@pytest.mark.asyncio
async def test_forged_console_cookie_does_not_arm_the_csrf_guard(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """A garbage console cookie leaves an unrelated cross-origin POST to normal validation instead of a CSRF 403."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        client.cookies.set(COOKIE_NAME, "garbage")
        response = await client.post(
            "/deposits/create",
            headers={"origin": "https://evil.example"},
            json={},
        )
        assert response.status_code != 403


@pytest.mark.asyncio
async def test_stale_cookie_for_an_old_username_reports_signed_out(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """GET /_sara/session treats a validly signed cookie for a former username as signed out."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        client.cookies.set(
            COOKIE_NAME, sign_console_session("former-owner", SECRET, 300)
        )
        response = await client.get("/_sara/session")
        assert response.status_code == 200
        assert response.json() == {
            "authenticated": False,
            "username": None,
            "enabled": True,
        }


@pytest.mark.asyncio
async def test_foreign_username_session_does_not_grant_admin_access(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    """A validly signed cookie for another username is not treated as an admin session by protected APIs."""
    async with _client(_auth_settings(test_settings), fake_clock) as client:
        client.cookies.set(
            COOKIE_NAME, sign_console_session("someone-else", SECRET, 300)
        )
        assert (await client.get("/dashboard/data")).status_code == 401
        assert (await client.get("/admin/config")).status_code == 401
        assert (await client.get("/_sara/session")).json()["authenticated"] is False
