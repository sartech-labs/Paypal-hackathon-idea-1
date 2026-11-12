"""Reverse-proxy mode: SARA in front of an existing website (ORIGIN_URL)."""

from __future__ import annotations

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.origin_proxy import OriginProxy, parse_robots_txt
from server.services.runtime_config import origin_status
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow

ORIGIN_ROBOTS = """
# The origin site's own robots.txt
User-agent: Googlebot
Disallow: /only-for-google

User-agent: *
Disallow: /account
Disallow: /checkout/*
Allow: /checkout/help
Crawl-delay: 5
"""


def origin_handler(seen: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path + (("?" + request.url.query.decode()) if request.url.query else ""))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ORIGIN_ROBOTS)
        if request.url.path == "/news":
            return httpx.Response(200, html="<h1>Origin news</h1>", headers={"Cache-Control": "max-age=60", "Set-Cookie": "sid=secret"})
        if request.url.path == "/blog/post-1":
            return httpx.Response(200, html="<h1>Post 1</h1>")
        if request.url.path == "/trailing/":
            return httpx.Response(200, html="<h1>Trailing slash</h1>")
        if request.url.path == "/broken":
            return httpx.Response(503, text="down")
        if request.url.path == "/error500":
            return httpx.Response(500, text="boom")
        return httpx.Response(404, text="not found")

    return handle


def test_parse_robots_takes_only_the_wildcard_group() -> None:
    rules = parse_robots_txt(ORIGIN_ROBOTS)
    assert rules.disallow == ["/account", "/checkout/"]
    assert rules.allow == ["/checkout/help"]
    assert rules.crawl_delay_seconds == 5.0
    assert "/only-for-google" not in rules.disallow


@pytest.fixture
def proxied(test_settings: Settings, fake_clock: FakeClock):
    seen: list[str] = []
    handler = origin_handler(seen)
    test_settings.origin_url = "https://origin.example"
    test_settings.rate_limit_requests = 5
    proxy = OriginProxy(
        test_settings.origin_url,
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    app = create_app(settings=test_settings, rate_limiter=SlidingWindowRateLimiter(clock=fake_clock), origin_proxy=proxy)
    return app, seen


async def test_proxy_forwards_allowed_paths_and_blocks_robots_disallows(proxied, fake_clock) -> None:
    app, seen = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        robots = (await client.get("/robots.txt")).text
        assert "Disallow: /account" in robots and "Crawl-delay: 5" in robots

        _, _, session = await complete_onboarding_flow(client, "crawler-proxy")
        auth = {"Authorization": f"Bearer {session['access_token']}"}

        # Any path the robots rules allow is forwarded, even one SARA has no copy of.
        res = await client.get("/gateway/blog/post-1?page=2", headers=auth)
        assert res.status_code == 200 and "Post 1" in res.text
        assert "/blog/post-1?page=2" in seen
        assert res.headers["X-SARA-Session-Id"] == session["session_id"]

        # Respect the origin's 5 s Crawl-delay between allowed requests.
        fake_clock.advance(5)
        res = await client.get("/gateway/news", headers=auth)
        assert res.status_code == 200 and "Origin news" in res.text
        assert res.headers["cache-control"] == "max-age=60"
        assert "set-cookie" not in res.headers

        # The origin's own Disallow lines are enforced before anything reaches the origin.
        before = len(seen)
        res = await client.get("/gateway/account", headers=auth)
        assert res.status_code == 403 and res.json()["reason_code"] == "DISALLOWED_PATH"
        assert len(seen) == before


async def test_proxy_preserves_trailing_slash_for_origin_canonicalization(proxied) -> None:
    app, seen = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-proxy-slash")
        auth = {"Authorization": f"Bearer {session['access_token']}"}
        res = await client.get("/gateway/trailing/", headers=auth)
        assert res.status_code == 200 and "Trailing slash" in res.text
        assert "/trailing/" in seen


async def test_proxy_passes_origin_404_and_5xx_statuses(proxied, fake_clock) -> None:
    app, _ = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-proxy-2")
        auth = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/missing", headers=auth)).status_code == 404
        fake_clock.advance(5)
        broken = await client.get("/gateway/broken", headers=auth)
        assert broken.status_code == 503 and broken.text == "down"
        fake_clock.advance(5)
        error = await client.get("/gateway/error500", headers=auth)
        assert error.status_code == 500 and error.text == "boom"


async def test_crawl_delay_is_published_separately_from_the_rate_window(proxied) -> None:
    app, _ = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        policy = (await client.get("/policies")).json()
        # The owner's rate window is untouched; Crawl-delay is its own constraint.
        assert policy["crawl_delay_seconds"] == 5
        assert policy["rate_limit_window_seconds"] == 10


async def test_crawl_delay_rejects_requests_that_are_too_close(proxied, fake_clock) -> None:
    app, _ = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-crawl-delay")
        auth = {"Authorization": f"Bearer {session['access_token']}"}

        assert (await client.get("/gateway/news", headers=auth)).status_code == 200
        fake_clock.advance(2)
        too_soon = await client.get("/gateway/news", headers=auth)
        assert too_soon.status_code == 429
        body = too_soon.json()
        assert body["reason_code"] == "RATE_LIMIT_EXCEEDED"
        assert "Crawl-delay" in body["reason"]
        assert body["retry_after_seconds"] >= 3

        fake_clock.advance(3)
        assert (await client.get("/gateway/news", headers=auth)).status_code == 200

def test_robots_redirect_is_not_followed() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /internal\n")

    proxy = OriginProxy("https://origin.example", sync_transport=httpx.MockTransport(handler))
    rules = proxy.import_robots()
    assert requested == ["/robots.txt"]
    assert rules.disallow == []
    assert proxy.last_error is None
    assert proxy.robots_imported is False


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_missing_or_forbidden_robots_is_not_unreachable(status: int) -> None:
    proxy = OriginProxy(
        "https://origin.example",
        sync_transport=httpx.MockTransport(lambda request: httpx.Response(status)),
    )
    proxy.import_robots()
    assert proxy.last_error is None
    assert proxy.robots_imported is False
    assert origin_status(proxy).reachable is True


def test_robots_5xx_is_still_unreachable() -> None:
    proxy = OriginProxy(
        "https://origin.example",
        sync_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    proxy.import_robots()
    assert proxy.last_error is not None
    assert origin_status(proxy).reachable is False


async def test_allowed_agent_post_forwards_method_body_and_headers(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    seen: list[tuple[str, str, bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        seen.append(
            (
                request.method,
                request.url.path,
                request.content,
                request.headers.get("content-type", ""),
            )
        )
        return httpx.Response(201, text="created")

    settings = test_settings.model_copy(
        update={
            "origin_url": "https://origin.example",
            "rate_limit_requests": 10,
            "rate_limit_window_seconds": 60.0,
        }
    )
    proxy = OriginProxy(
        settings.origin_url,
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=fake_clock), origin_proxy=proxy)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-proxy-post")
        auth = {"Authorization": f"Bearer {session['access_token']}"}
        response = await client.post(
            "/submit",
            content="payload=1",
            headers={
                **auth,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 201
        assert seen[-1] == (
            "POST",
            "/submit",
            b"payload=1",
            "application/x-www-form-urlencoded",
        )


@pytest.mark.asyncio
async def test_forward_never_leaks_the_owner_console_cookie() -> None:
    from server.core.owner_auth import COOKIE_NAME
    from server.services.origin_proxy import CONSOLE_COOKIE

    # The proxy's name for the cookie must stay the sign-in module's name.
    assert CONSOLE_COOKIE == COOKIE_NAME

    received: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            text="ok",
            headers=[
                ("set-cookie", f"{COOKIE_NAME}=forged; Path=/"),
                ("set-cookie", "site_session=abc; Path=/"),
                ("content-type", "text/plain"),
            ],
        )

    proxy = OriginProxy("https://origin.example", transport=httpx.MockTransport(handler))

    mixed = await proxy.forward("GET", "/news", headers={"Cookie": f"site_session=abc; {COOKIE_NAME}=secret; theme=dark"})
    only_ours = await proxy.forward("GET", "/news", headers={"Cookie": f"{COOKIE_NAME}=secret"})

    # The owner's site keeps its own cookies but never sees the console session.
    assert received[0] == "site_session=abc; theme=dark"
    assert received[1] is None
    # And it cannot set or clear the console session through the proxy.
    set_cookies = [value for key, value in mixed.headers.multi_items() if key.lower() == "set-cookie"]
    assert set_cookies == ["site_session=abc; Path=/"]
    assert only_ours.status_code == 200


def _redirecting(status: int, location: str | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"location": location} if location else {}
        return httpx.Response(status, headers=headers, text="Redirecting...")

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("origin", "status", "location", "expected"),
    [
        ("https://example.com", 308, "https://www.example.com/", "https://www.example.com"),
        ("https://www.example.com", 301, "https://example.com/", "https://example.com"),
        ("http://example.com", 301, "https://example.com/", "https://example.com"),
        ("https://example.com", 302, "/home", "https://example.com"),
        ("https://example.com", 302, "https://evil.example.net/", "https://example.com"),
        ("https://example.com", 302, "https://example.com.evil.net/", "https://example.com"),
        ("https://example.com", 200, None, "https://example.com"),
        ("https://example.com", 308, None, "https://example.com"),
    ],
)
def test_canonical_origin_follows_only_same_site_redirects(origin, status, location, expected) -> None:
    from server.services.origin_proxy import canonical_origin

    assert canonical_origin(origin, transport=_redirecting(status, location)) == expected


def test_canonical_origin_keeps_the_address_when_the_site_is_unreachable() -> None:
    from server.services.origin_proxy import canonical_origin

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    assert canonical_origin("https://example.com", transport=httpx.MockTransport(handler)) == "https://example.com"
