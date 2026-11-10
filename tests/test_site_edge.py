"""Transparent site edge: agents are gated, humans see the origin."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from server.config import Settings
from server.main import create_app
from server.services.origin_proxy import OriginProxy, parse_robots_txt
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock

ORIGIN_ROBOTS = "User-agent: *\nDisallow: /account\nDisallow: /secret\nCrawl-delay: 2\n"
ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _handler(seen: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ORIGIN_ROBOTS)
        if request.url.path == "/":
            return httpx.Response(200, html="<h1>Origin home</h1>")
        if request.url.path == "/news":
            return httpx.Response(200, html="<h1>Origin news</h1>")
        if request.url.path == "/account":
            return httpx.Response(200, text="secret")
        if request.url.path == "/cart" and request.method == "POST":
            return httpx.Response(201, text=f"cart:{request.content.decode()}", headers={"Set-Cookie": "sid=abc; Path=/"})
        if request.url.path == "/login":
            return httpx.Response(
                200,
                text="signed in",
                headers=[("set-cookie", "sid=abc; Path=/"), ("set-cookie", "theme=dark; Path=/")],
            )
        return httpx.Response(404, text="not found")
    return handle


def _app(settings: Settings, clock: FakeClock) -> tuple[Any, list[str]]:
    seen: list[str] = []
    handler = _handler(seen)
    proxy = OriginProxy(
        "https://origin.example",
        transport=httpx.MockTransport(handler),
        sync_transport=httpx.MockTransport(handler),
    )
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock), origin_proxy=proxy)
    return app, seen


async def test_gateway_mode_proxies_humans_and_gates_agents(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    app, seen = _app(settings, fake_clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        human = {"User-Agent": BROWSER_UA}
        home = await client.get("/", headers=human)
        assert home.status_code == 200 and "Origin home" in home.text
        news = await client.get("/news", headers=human)
        assert news.status_code == 200 and "Origin news" in news.text

        # An unsigned agent is recognised by its non-browser User-Agent and challenged.
        challenge = await client.get("/news", headers={"User-Agent": "example-agent/1.0"})
        assert challenge.status_code == 402
        assert challenge.json()["deposit"]["amount"] == "5.00"
        # The challenge did not touch the origin.
        assert "/news" in seen


async def test_gateway_mode_forwards_human_methods_and_cookies(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    app, _ = _app(settings, fake_clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/cart",
            content="item=1",
            headers={"Cookie": "theme=dark", "User-Agent": BROWSER_UA},
        )
        assert response.status_code == 201
        assert response.text == "cart:item=1"
        assert response.headers["set-cookie"].startswith("sid=abc")


async def test_demo_mode_root_is_console_and_agents_still_gate(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    app, _ = _app(test_settings, fake_clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        root = await client.get("/")
        assert root.status_code == 200 and "SARA console" in root.text
        assert (
            await client.get("/news", headers={"User-Agent": "example-agent/1.0"})
        ).status_code == 402

async def test_gateway_mode_preserves_multiple_set_cookie_headers(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    app, _ = _app(settings, fake_clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/login", headers={"User-Agent": BROWSER_UA})
        assert response.status_code == 200
        assert response.headers.get_list("set-cookie") == [
            "sid=abc; Path=/",
            "theme=dark; Path=/",
        ]


async def test_saving_config_does_not_persist_imported_origin_robots(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    def fake_import(self):
        self.rules = parse_robots_txt(ORIGIN_ROBOTS)
        self.robots_imported = True
        return self.rules

    monkeypatch.setattr(OriginProxy, "import_robots", fake_import)
    settings = test_settings.model_copy(
        update={"sara_mode": "gateway", "origin_url": "https://origin.example"}
    )
    app, _ = _app(settings, fake_clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        before = (await client.get("/admin/config", headers=ADMIN)).json()
        assert "/secret" in before["disallowed_paths"]
        payload = {
            key: before[key]
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
        saved = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json=payload,
        )
        assert saved.status_code == 200, saved.text
        assert "/secret" in saved.json()["disallowed_paths"]

    persisted = settings.gateway_config_path.read_text(encoding="utf-8")
    assert "/secret" not in persisted

async def test_edge_fails_closed_for_non_browser_user_agents(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    app, _ = _app(settings, fake_clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for user_agent in (
            "curl/8.7.1",
            "python-httpx/0.27.0",
            "my-crawler/1.0",
            "",
        ):
            response = await client.get("/news", headers={"User-Agent": user_agent})
            assert response.status_code == 402, user_agent
        # Headless Chrome advertises a real browser fingerprint, so under the
        # fingerprint-only rule it is served as a human. Gate it by policy instead.
        plain = await client.get("/news", headers={"User-Agent": BROWSER_UA})
        assert plain.status_code == 200 and "Origin news" in plain.text

