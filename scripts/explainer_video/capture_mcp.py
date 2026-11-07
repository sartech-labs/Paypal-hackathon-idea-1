"""Run a real MCP session against the storefront and save the calls for the video."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.client import Client


async def call(client: Client, name: str, args: dict) -> dict:
    result = await client.call_tool(name, args)
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if not data:
        data = json.loads("".join(getattr(p, "text", "") for p in result.content))
    return data


async def main(url: str, out: Path) -> None:
    log = []
    async with Client(url) as client:
        tools = [t.name for t in (await client.list_tools()).tools]
        offers = await call(client, "discover_offers", {})
        wallet = offers["wallet_id"]
        log.append({"tool": "discover_offers", "args": {}, "result": {"merchant": offers["merchant"], "items": len(offers["offers"]["pay_per_access"]), "products": len(offers["offers"]["products"])}})
        quote = await call(client, "quote_access", {"item_id": "agent-traffic-dataset", "reason": "User wants the agent traffic data", "wallet_id": wallet})
        log.append({"tool": "quote_access", "args": {"item_id": "agent-traffic-dataset"}, "result": {k: quote.get(k) for k in ("order_id", "amount", "currency", "status", "provider_order_id")}})
        paid = await call(client, "pay_order", {"order_id": quote["order_id"], "wallet_id": wallet})
        log.append({"tool": "pay_order", "args": {"order_id": quote["order_id"]}, "result": {k: paid.get(k) for k in ("status", "provider_order_id", "provider_capture_id", "unlocked")}})
        content = await call(client, "open_paid_content", {"path": "/media/data/agent-traffic-q3", "wallet_id": wallet})
        log.append({"tool": "open_paid_content", "args": {"path": "/media/data/agent-traffic-q3"}, "result": {"status": content.get("status"), "preview": (content.get("content") or "")[:260]}})
    out.write_text(json.dumps({"tools": tools, "calls": log}, indent=2), encoding="utf-8")
    print(json.dumps(log, indent=1))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011/mcp/", Path(sys.argv[2] if len(sys.argv) > 2 else "video-out/mcp_session.json")))
