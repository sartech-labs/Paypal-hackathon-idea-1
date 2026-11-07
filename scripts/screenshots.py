"""Capture a set of product screenshots from a running SARA gateway (ideally the PayPal Sandbox).

Agent studio: the example requests, a live assistant run (deposit, an approval card, the answer and
the money panel), the rule-breaking scraper and the website's verdict, the offer catalog, and the
chat on a phone. Publisher console: overview, a kept deposit's AI explanation, security,
commerce, Verify with PayPal (PayPal's MCP server), and AG Studio.

    python scripts/screenshots.py --base http://127.0.0.1:8011 --out docs/images

Starts real runs (the assistant needs an AI key). It approves one-off purchases and declines
subscriptions, which would need a person to approve at PayPal.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

W, H = 1920, 1080


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--out", default="docs/images")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api = httpx.Client(base_url=args.base, timeout=30)

    def shot(page: Page, name: str) -> None:
        page.wait_for_timeout(700)
        page.screenshot(path=str(out / f"{name}.png"))
        print("saved", name, flush=True)

    def left_to(page: Page, selector: str, block: str = "center") -> None:
        page.evaluate("([s, b]) => { const n = document.querySelector(s); if (n) n.scrollIntoView({block: b}); }", [selector, block])
        page.wait_for_timeout(500)

    def active_run() -> str:
        for _ in range(100):
            run_id = api.get("/demo/config").json().get("active_run_id")
            if run_id:
                return run_id
            time.sleep(0.2)
        raise RuntimeError("the run did not start")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme="dark")
        page = ctx.new_page()

        # --- 1. Agent studio: the example requests -------------------------------------------------
        page.goto(f"{args.base}/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        shot(page, "01-agent-studio-start")

        # --- 2-5. The assistant: read the news with a deposit, buy, ask, answer --------------------
        page.locator(".suggestion").first.click()
        run_id = active_run()
        seen, took_deposit, took_ask = 0, False, False
        while True:
            snap = api.get(f"/demo/runs/{run_id}?after={seen}").json()
            for e in snap["events"]:
                if e["kind"] == "tool_result" and e["data"]["name"] == "fetch_page" and not took_deposit and (e["data"]["result"] or {}).get("status") == 200:
                    page.wait_for_timeout(1500)
                    left_to(page, '.stage[data-stage="deposit"]')
                    shot(page, "02-agent-deposit-and-rules")
                    took_deposit = True
            seen = snap["next_seq"]
            if snap["status"] == "awaiting_decision":
                pending = snap["pending_decision"] or {}
                page.wait_for_selector("#approve-button", timeout=20000)
                left_to(page, '.stage[data-stage="buy"]')
                if not took_ask:
                    shot(page, "03-agent-asks-before-buying")
                    took_ask = True
                page.click("#decline-button" if pending.get("payment_type") == "subscription" else "#approve-button")
                page.wait_for_timeout(1500)
            if snap["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.8)
        page.wait_for_timeout(2500)
        left_to(page, '.stage[data-stage="settle"]')
        shot(page, "04-agent-answer")
        left_to(page, ".wallet", "start")
        shot(page, "05-agent-payments-with-paypal-references")

        # --- 6-7. Another agent: the rule-breaking scraper ------------------------------------------
        page.click("#new-chat")
        page.wait_for_timeout(800)
        page.locator(".suggestion.rogue-pick").click()
        run_id = active_run()
        took_blocked = False
        while True:
            snap = api.get(f"/demo/runs/{run_id}").json()
            blocked = sum(1 for e in snap["events"] if e["kind"] == "tool_result" and (e["data"]["result"] or {}).get("status") in (403, 429))
            if blocked >= 4 and not took_blocked:
                page.wait_for_timeout(800)
                left_to(page, '.stage[data-stage="crawl"]')
                shot(page, "06-scraper-blocked")
                took_blocked = True
            if snap["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.5)
        page.wait_for_timeout(3000)
        left_to(page, '.stage[data-stage="settle"]')
        shot(page, "07-scraper-deposit-kept-with-ai-explanation")

        # --- 8. What the site sells (and PayPal buttons for people) --------------------------------
        left_to(page, ".offer-strip", "start")
        shot(page, "08-site-offers-for-agents")

        # --- 9. On a phone ------------------------------------------------------------------------------
        phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
        p2 = phone.new_page()
        p2.goto(f"{args.base}/", wait_until="networkidle")
        p2.wait_for_timeout(1500)
        p2.screenshot(path=str(out / "09-agent-studio-on-a-phone.png"))
        print("saved 09-agent-studio-on-a-phone", flush=True)
        phone.close()

        # --- 10-15. The website owner's console -------------------------------------------------------
        def nav(view: str) -> None:
            page.locator(f".sidenav-link[data-view='{view}']").click()
            page.wait_for_timeout(2000)
            page.evaluate("scrollTo(0, 0)")

        page.goto(f"{args.base}/console#overview", wait_until="networkidle")
        page.wait_for_timeout(3000)
        shot(page, "10-owner-overview-ag-grid")
        page.locator(".ag-row", has_text="Forfeited").first.click()
        page.wait_for_timeout(4000)
        page.evaluate("(() => { const n = document.querySelector('#explanation') || document.querySelector('.inspector'); if (n) n.scrollIntoView({block: 'center'}); })()")
        shot(page, "11-owner-why-the-deposit-was-kept")

        nav("security")
        page.wait_for_timeout(1500)
        shot(page, "12-owner-security-violations")

        nav("commerce")
        page.wait_for_timeout(3000)
        shot(page, "13-owner-commerce-revenue-and-orders")
        page.evaluate("document.querySelector('#grid-orders').scrollIntoView({block: 'start'})")
        page.wait_for_timeout(800)
        page.locator("#grid-orders button", has_text="Verify").first.click()
        page.wait_for_selector("#order-verify table", timeout=120000)
        page.evaluate("document.querySelector('#order-verify').scrollIntoView({block: 'center'})")
        page.wait_for_timeout(1500)
        shot(page, "14-owner-verify-with-paypal-mcp")

        nav("analytics")
        page.get_by_role("button", name="Agent commerce").click()
        page.wait_for_timeout(9000)
        shot(page, "15-owner-ag-studio-analytics")

        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
