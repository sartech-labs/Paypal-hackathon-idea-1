"""Record the rule-breaking scraper scenario in Agent studio: a rogue bot hits the SARA gateway live.

It pays the refundable deposit through PayPal, then tries /admin, private data, a path trick and
request bursts; the gateway answers 403/429, and the website's verdict (deposit kept, with the AI
explanation) appears in the chat. The left side follows the steps.
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
    ap.add_argument("--out", default="video-out/rec_crawler")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api = httpx.Client(base_url=args.base, timeout=30)
    marks: dict[str, float] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(out / "raw"),
                                  record_video_size={"width": W, "height": H}, color_scheme="dark")
        page = ctx.new_page()
        t0 = time.monotonic()

        def mark(name: str) -> None:
            marks[name] = round(time.monotonic() - t0, 2)
            print(f"{marks[name]:7.2f}s  {name}", flush=True)

        def left_to(selector: str, block: str = "center") -> None:
            page.evaluate("([s, b]) => { const n = document.querySelector(s); if (n) n.scrollIntoView({block: b, behavior: 'smooth'}); }", [selector, block])

        page.goto(f"{args.base}/", wait_until="networkidle")
        page.wait_for_timeout(2000)
        mark("loaded")
        page.locator(".suggestion.rogue-pick").scroll_into_view_if_needed()
        page.wait_for_timeout(1200)
        mark("configured")
        page.locator(".suggestion.rogue-pick").click()
        mark("started")
        run_id = None
        for _ in range(100):
            run_id = api.get("/demo/config").json().get("active_run_id")
            if run_id:
                break
            time.sleep(0.2)
        while True:
            snap = api.get(f"/demo/runs/{run_id}").json()
            if snap["status"] in ("completed", "failed", "cancelled"):
                break
            left_to('.stage[data-state="active"]')
            time.sleep(0.5)
        page.wait_for_selector(".msg.sara", timeout=60000)
        mark("finished")
        left_to('.stage[data-stage="settle"]')
        page.wait_for_timeout(5000)
        mark("summary")
        left_to(".wallet", "start")
        page.wait_for_timeout(4000)
        mark("end")
        video_path = page.video.path()
        ctx.close()
        browser.close()
    shutil.copyfile(video_path, out / "crawler.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
