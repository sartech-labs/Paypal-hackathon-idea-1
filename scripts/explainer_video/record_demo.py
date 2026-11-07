"""Record the live SARA demo for the explainer video.

Drives a real browser (Playwright, 1536x864) against a running gateway, answers the
agent's approval prompts like a person would, and writes:
  <out>/demo.webm        the screen recording
  <out>/marks.json       seconds-from-start of each moment, for the editor
  <out>/facts.json       what actually happened (PayPal IDs, outcomes), for the script

Usage: python scripts/explainer_video/record_demo.py --base http://127.0.0.1:8011 --out video-out/rec
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

W, H = 1536, 864


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--out", default="video-out/rec")
    ap.add_argument("--decline", default="subscription", help="payment type to decline (others approved)")
    ap.add_argument("--task", default="Catch me up on today's news from Sentinel News, and get me whatever they have on how AI agents pay websites: videos, guides, anything useful.")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api = httpx.Client(base_url=args.base, timeout=30)
    marks: dict[str, float] = {}
    facts: dict = {"decisions": []}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(out / "raw"), record_video_size={"width": W, "height": H}, color_scheme="dark")
        page = ctx.new_page()
        t0 = time.monotonic()

        def mark(name: str) -> None:
            marks[name] = round(time.monotonic() - t0, 2)
            print(f"{marks[name]:7.2f}s  {name}", flush=True)

        def smooth_scroll(y: int, ms: int = 900) -> None:
            page.evaluate(
                """([y, ms]) => new Promise(r => { const s = scrollY, d = y - s, t0 = performance.now();
                   const f = (t) => { const k = Math.min(1, (t - t0) / ms); scrollTo(0, s + d * (1 - Math.cos(Math.PI * k)) / 2);
                   k < 1 ? requestAnimationFrame(f) : r(); }; requestAnimationFrame(f); })""",
                [y, ms],
            )

        def pane_scroll(selector: str, block: str = "center") -> None:
            # The page itself does not scroll: each pane does. Bring an element into view in its pane.
            page.evaluate("([sel, block]) => { const n = document.querySelector(sel); if (n) n.scrollIntoView({block, behavior: 'smooth'}); }", [selector, block])

        page.goto(f"{args.base}/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        mark("studio_loaded")
        page.wait_for_timeout(3500)
        page.click("#task")
        page.keyboard.type(args.task, delay=28)
        mark("task_filled")
        page.wait_for_timeout(1200)
        page.keyboard.press("Enter")
        mark("run_started")

        run_id = None
        for _ in range(50):
            cfg = api.get("/demo/config").json()
            run_id = cfg.get("active_run_id")
            if run_id:
                break
            time.sleep(0.2)
        facts["run_id"] = run_id

        seen_events = 0
        deadline = time.monotonic() + 900
        status = "running"
        while time.monotonic() < deadline:
            snap = api.get(f"/demo/runs/{run_id}?after={seen_events}").json()
            for e in snap["events"]:
                d = e["data"]
                if e["kind"] == "tool_call":
                    mark(f"call:{d['name']}:{len([k for k in marks if k.startswith('call:' + d['name'])])}")
                elif e["kind"] == "purchase_review":
                    mark(f"review:{d.get('verdict')}:{d.get('title', '')[:40]}")
            seen_events = snap["next_seq"]
            status = snap["status"]
            if status == "awaiting_decision":
                pending = snap["pending_decision"] or {}
                approve = pending.get("payment_type") != args.decline
                page.wait_for_selector("#approve-button", timeout=15000)
                pane_scroll('.stage[data-stage="buy"]')
                mark(f"decision_shown:{pending.get('payment_type')}")
                page.wait_for_timeout(3200)  # let the viewer read the card
                page.click("#approve-button" if approve else "#decline-button")
                mark(f"decision_{'approved' if approve else 'declined'}:{pending.get('payment_type')}")
                facts["decisions"].append({"type": pending.get("payment_type"), "title": pending.get("title"), "amount": pending.get("amount"), "approved": approve, "review": pending.get("review")})
                page.wait_for_timeout(1500)
            elif status in ("completed", "failed", "cancelled"):
                break
            else:
                # Keep the explanation on the left on the step that is happening now.
                pane_scroll('.stage[data-state="active"]')
            time.sleep(0.6)
        mark("run_finished")
        final = api.get(f"/demo/runs/{run_id}").json()
        facts["status"] = final["status"]
        facts["final_text"] = final.get("final_text")
        facts["mandate"] = final.get("mandate")
        facts["usage"] = final.get("usage")
        page.wait_for_timeout(1500)
        pane_scroll('.stage[data-stage="settle"]')
        mark("report")
        page.wait_for_timeout(6000)
        pane_scroll(".wallet", "start")
        mark("wallet")
        page.wait_for_timeout(4500)

        # Publisher console: commerce view, then Verify with PayPal on a paid order.
        page.goto(f"{args.base}/console#commerce", wait_until="networkidle")
        page.wait_for_timeout(1500)
        mark("console_commerce")
        page.wait_for_timeout(4000)
        grid_y = page.evaluate("document.querySelector('#grid-orders').getBoundingClientRect().top + scrollY - 140")
        smooth_scroll(int(grid_y), 1000)
        mark("orders_grid")
        page.wait_for_timeout(2500)
        paid_rows = page.locator("#grid-orders .ag-center-cols-container .ag-row")
        verify_buttons = page.locator("#grid-orders .ag-pinned-right-cols-container .cell-button", has_text="Verify")
        if verify_buttons.count():
            verify_buttons.first.click()
            mark("verify_clicked")
            page.wait_for_selector("#order-verify table", timeout=90000)
            mark("verify_result")
            facts["verify_text"] = page.inner_text("#order-verify")
            page.wait_for_timeout(5500)
        facts["paid_rows"] = paid_rows.count()

        page.goto(f"{args.base}/console#analytics", wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.get_by_role("button", name="Agent commerce").click()
        mark("analytics_commerce")
        page.wait_for_timeout(7000)
        mark("end")
        video_path = page.video.path()
        ctx.close()
        browser.close()

    shutil.copyfile(video_path, out / "demo.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")
    (out / "facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
    print("saved", out / "demo.webm")


if __name__ == "__main__":
    main()
