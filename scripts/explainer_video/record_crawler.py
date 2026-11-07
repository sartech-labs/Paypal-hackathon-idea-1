"""Record the crawler demo page: a rogue scraper hits the SARA gateway live.

It pays the refundable deposit through PayPal, then tries /admin, private data, a path trick and
request bursts; the gateway answers 403/429, and the deposit is kept at the end.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

W, H = 1536, 864


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--out", default="video-out/rec_crawler")
    ap.add_argument("--persona", default="rogue")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
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

        page.goto(f"{args.base}/crawler", wait_until="networkidle")
        page.wait_for_timeout(2000)
        mark("loaded")
        page.locator("label.choice", has=page.locator(f"input[name='persona'][value='{args.persona}']")).click()
        page.wait_for_timeout(900)
        page.locator("label", has=page.locator("#driver-scripted")).first.click()
        page.wait_for_timeout(900)
        mark("configured")
        page.locator("#start-button").click()
        mark("started")
        # Keep the live timeline in view while it runs.
        page.evaluate("document.querySelector('#run-title').scrollIntoView({block: 'start', behavior: 'smooth'})")
        page.wait_for_function("document.querySelector('#run-state')?.dataset.state && !['running','starting','waiting'].includes(document.querySelector('#run-state').dataset.state) && document.querySelector('#run-state').dataset.state !== 'idle'", timeout=180000)
        mark("finished")
        page.wait_for_timeout(2500)
        summary = page.locator("#run-summary")
        if summary.count() and summary.is_visible():
            summary.scroll_into_view_if_needed()
        page.wait_for_timeout(5000)
        mark("summary")
        page.evaluate("window.scrollBy({top: 700, behavior: 'smooth'})")
        page.wait_for_timeout(4000)
        mark("end")
        video_path = page.video.path()
        ctx.close()
        browser.close()
    shutil.copyfile(video_path, out / "crawler.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
