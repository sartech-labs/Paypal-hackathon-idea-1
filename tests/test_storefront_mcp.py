"""MCP storefront: the official MCP client shops the site over Streamable HTTP."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, AsyncIterator

import pytest
import uvicorn
from mcp.client.client import Client

from server.config import Settings
from server.main import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def mcp_url(test_settings: Settings) -> AsyncIterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings=test_settings), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.025)
    yield f"http://127.0.0.1:{port}/mcp/"
    server.should_exit = True
    await task


async def _call(client: Client, name: str, args: dict[str, Any] | None = None, wallet: str | None = None) -> dict[str, Any]:
    payload = dict(args or {})
    if wallet:
        payload["wallet_id"] = wallet
    result = await client.call_tool(name, payload)
    if getattr(result, "is_error", False):
        return {"tool_error": "".join(getattr(part, "text", "") for part in result.content)}
    if getattr(result, "structured_content", None):
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    text = "".join(getattr(part, "text", "") for part in result.content)
    return json.loads(text)


async def test_any_mcp_client_can_buy_read_and_crawl(mcp_url: str) -> None:
    async with Client(mcp_url) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"discover_offers", "quote_access", "pay_order", "subscribe", "open_paid_content", "start_crawl", "crawl_page", "finish_crawl"} <= names

        catalog = await _call(client, "discover_offers")
        assert catalog["your_agent_id"].startswith("mcp-") and catalog["offers"]["pay_per_access"]
        w = catalog["wallet_id"]

        locked = await _call(client, "open_paid_content", {"path": "/media/video/agent-economy-briefing"}, wallet=w)
        assert locked["status"] == 402 and locked["offers"][0]["item_id"] == "agent-economy-video"

        quote = await _call(client, "quote_access", {"item_id": "agent-economy-video", "reason": "my user asked for it"}, wallet=w)
        assert quote["status"] == "CREATED" and quote["amount"] == "2.00"
        paid = await _call(client, "pay_order", {"order_id": quote["order_id"]}, wallet=w)
        assert paid["status"] == "PAID" and paid["unlocked"] == ["/media/video/agent-economy-briefing"]
        assert "access_token" not in json.dumps(paid)  # the token stays on the server
        opened = await _call(client, "open_paid_content", {"path": "/media/video/agent-economy-briefing"}, wallet=w)
        assert opened["status"] == 200 and "<untrusted_page_content>" in opened["content"]

        sub = await _call(client, "subscribe", {"plan_id": "sentinel-pro-monthly", "reason": "daily feed"}, wallet=w)
        assert sub["status"] == "ACTIVE"
        feed = await _call(client, "open_paid_content", {"path": "/members/daily-feed"}, wallet=w)
        assert feed["status"] == 200

        crawl = await _call(client, "start_crawl", {"path": "/news"}, wallet=w)
        assert crawl["deposit"]["payment_status"] == "CAPTURED" and crawl["deposit"]["required_amount"] == "1.00"
        page = await _call(client, "crawl_page", {"path": "/news"}, wallet=w)
        assert page["status"] == 200
        blocked = await _call(client, "crawl_page", {"path": "/admin"}, wallet=w)
        assert blocked["blocked"] is True
        done = await _call(client, "finish_crawl", None, wallet=w)
        assert done["deposit_state"] == "FORFEITED"  # requesting /admin breaks the policy

    # A different MCP session is a different shopper with an empty wallet.
    async with Client(mcp_url) as other:
        fresh = (await _call(other, "discover_offers"))["wallet_id"]
        assert fresh != w
        again = await _call(other, "open_paid_content", {"path": "/media/video/agent-economy-briefing"}, wallet=fresh)
        assert again["status"] == 402
        unknown = await _call(other, "pay_order", {"order_id": "ord_x"}, wallet="not-a-wallet")
        assert "Unknown wallet_id" in unknown["tool_error"]


async def test_compliant_mcp_crawl_gets_the_deposit_back(mcp_url: str) -> None:
    async with Client(mcp_url) as client:
        w = (await _call(client, "discover_offers"))["wallet_id"]
        await _call(client, "start_crawl", {"path": "/news"}, wallet=w)
        assert (await _call(client, "crawl_page", {"path": "/news/technology"}, wallet=w))["status"] == 200
        done = await _call(client, "finish_crawl", None, wallet=w)
        assert done["deposit_state"] == "REFUNDED"
