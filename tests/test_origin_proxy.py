"""Reverse-proxy mode: SARA in front of an existing website (ORIGIN_URL)."""

from __future__ import annotations

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.origin_proxy import OriginProxy, parse_robots_txt
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
        if request.url.path == "/broken":
            return httpx.Response(503, text="down")
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


async def test_proxy_forwards_allowed_paths_and_blocks_robots_disallows(proxied) -> None:
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

        res = await client.get("/gateway/news", headers=auth)
        assert res.status_code == 200 and "Origin news" in res.text
        assert res.headers["cache-control"] == "max-age=60"
        assert "set-cookie" not in res.headers

        # The origin's own Disallow lines are enforced before anything reaches the origin.
        before = len(seen)
        res = await client.get("/gateway/account", headers=auth)
        assert res.status_code == 403 and res.json()["reason_code"] == "DISALLOWED_PATH"
        assert len(seen) == before


async def test_proxy_passes_origin_404_and_hides_origin_errors(proxied) -> None:
    app, _ = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        _, _, session = await complete_onboarding_flow(client, "crawler-proxy-2")
        auth = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/missing", headers=auth)).status_code == 404
        res = await client.get("/gateway/broken", headers=auth)
        assert res.status_code == 502 and res.json()["error"] == "origin_unavailable"


async def test_crawl_delay_slows_the_rate_limit(proxied) -> None:
    app, _ = proxied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        policy = (await client.get("/policies")).json()
        text = str(policy)
        # 5 requests per window with a 5 s crawl delay: the window grows to 25 s.
        assert "25" in text
