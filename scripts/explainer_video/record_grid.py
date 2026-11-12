"""Record the owner's AG Grid tables and AG Studio analytics on a gateway with plenty of activity.

Deposits grid: segment filters (Forfeited, Returned), sort by a column, expand a row's PayPal
details. Violations grid. Then Money -> Analytics: the AG Studio dashboards, page by page.

Usage: python scripts/explainer_video/record_grid.py --base http://127.0.0.1:8014
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
    ap.add_argument("--base", default="http://127.0.0.1:8014")
    ap.add_argument("--out", default="video-out/rec_grid")
    args = ap.parse_args()
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    marks: dict[str, float] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(out / "raw"),
                                  record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        t0 = time.monotonic()

        def mark(name: str) -> None:
            marks[name] = round(time.monotonic() - t0, 2)
            print(f"{marks[name]:8.2f}s  {name}", flush=True)

        def nav(view: str) -> None:
            link = page.locator(f".sidenav-link[data-view='{view}']")
            if not link.is_visible():
                for toggle in page.locator(".sidenav-parent").all():
                    toggle.click()
                    page.wait_for_timeout(250)
                    if link.is_visible():
                        break
            link.click()
            page.wait_for_timeout(1500)
            page.evaluate("scrollTo(0, 0)")

        def segment(view: str, label: str) -> None:
            page.locator(f"section.view[data-view='{view}'] button", has_text=label).first.click()
            page.wait_for_timeout(2200)

        page.goto(f"{args.base}/console", wait_until="networkidle")
        page.wait_for_timeout(2000)
        mark("start")

        # AG Grid: the deposits table.
        nav("deposits")
        mark("deposits")
        page.wait_for_timeout(2500)
        segment("deposits", "Forfeited")
        mark("filter_forfeited")
        segment("deposits", "Returned")
        mark("filter_returned")
        segment("deposits", "All")
        page.locator("#grid-deposits .ag-header-cell", has_text="Deposit").first.click()
        page.wait_for_timeout(1800)
        mark("sorted")
        page.locator("#grid-deposits .expand-button").first.click()
        page.wait_for_timeout(3000)
        mark("expanded")

        # AG Grid: violations, grouped by the rule that was broken.
        nav("security")
        mark("violations")
        page.wait_for_timeout(4000)

        # AG Studio: the analytics dashboards.
        nav("analytics")
        mark("analytics_open")
        page.wait_for_selector("#studio-status[hidden]", state="attached", timeout=90000)
        page.wait_for_timeout(4000)
        mark("analytics_overview")
        page.wait_for_timeout(3000)
        for label, key in (("Deposits & refunds", "finance"), ("Security", "security"), ("AI agents", "agents")):
            page.locator("#studio-pages button", has_text=label).click()
            page.wait_for_timeout(1000)
            mark(f"analytics_{key}")
            page.wait_for_timeout(4500)
        mark("end")
        video_path = page.video.path()
        ctx.close()
        browser.close()
    shutil.copyfile(video_path, out / "grid.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")
    print("saved", out)


if __name__ == "__main__":
    main()
