"""Record the website owner's view of the publisher console (AG Grid tables throughout).

Order: Overview (deposits grid + AI explanation of a kept deposit) -> Security (violations grid,
audit feed filtered to violations) -> Operations (sessions grid) -> Commerce (revenue tiles,
orders grid filtered, Verify with PayPal through PayPal's MCP server) -> AG Studio "Agent commerce".
Needs a gateway with a compliant run, a rogue run and some purchases on it.
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
    ap.add_argument("--out", default="video-out/rec_owner")
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

        def scroll_to(y: int, ms: int = 1000) -> None:
            page.evaluate(
                """([y, ms]) => new Promise(r => { const s = scrollY, d = y - s, t0 = performance.now();
                   const f = (t) => { const k = Math.min(1, (t - t0) / ms); scrollTo(0, s + d * (1 - Math.cos(Math.PI * k)) / 2);
                   k < 1 ? requestAnimationFrame(f) : r(); }; requestAnimationFrame(f); })""",
                [y, ms],
            )

        def top_of(sel: str, offset: int = 120) -> int:
            return int(page.evaluate(f"document.querySelector({json.dumps(sel)}).getBoundingClientRect().top + scrollY - {offset}"))

        def nav(view: str) -> None:
            page.locator(f".sidenav-link[data-view='{view}']").click()
            page.wait_for_timeout(1500)
            page.evaluate("scrollTo(0, 0)")

        page.goto(f"{args.base}/console#overview", wait_until="networkidle")
        page.wait_for_timeout(2500)
        mark("overview")
        page.wait_for_timeout(3000)
        # The kept (forfeited) deposit: its AI explanation in the inspector.
        page.locator(".ag-row", has_text="Forfeited").first.click()
        page.wait_for_timeout(3500)
        mark("overview_explained")

        nav("security")
        mark("security")
        page.wait_for_timeout(4000)
        feed = page.locator("button", has_text="Violation").last
        scroll_to(top_of("section.view[data-view='security'] .card:nth-of-type(2)", 80) if page.locator("section.view[data-view='security'] .card").count() > 1 else 600)
        page.wait_for_timeout(800)
        if feed.count():
            feed.click()
        mark("security_feed")
        page.wait_for_timeout(3500)

        nav("operations")
        mark("operations")
        page.wait_for_timeout(3500)

        nav("commerce")
        mark("commerce")
        page.wait_for_timeout(4500)
        scroll_to(top_of("#grid-orders", 150), 1100)
        mark("orders")
        page.wait_for_timeout(1500)
        chips = page.locator("section.view[data-view='commerce'] button", has_text="Products")
        if chips.count():
            chips.first.click()
            page.wait_for_timeout(2000)
            page.locator("section.view[data-view='commerce'] button", has_text="All").first.click()
            page.wait_for_timeout(1200)
        row = page.locator("#grid-orders .ag-row", has_text="Playbook").first
        idx = row.get_attribute("row-index")
        btn = page.locator(f"#grid-orders .ag-row[row-index='{idx}'] button", has_text="Verify").first
        if not btn.count():
            btn = page.locator("#grid-orders button", has_text="Verify").first
        btn.click()
        mark("verify_clicked")
        page.wait_for_selector("#order-verify table", timeout=120000)
        mark("verify_result")
        page.evaluate("document.querySelector('#order-verify').scrollIntoView({block: 'center', behavior: 'smooth'})")
        page.wait_for_timeout(5500)
        verify_text = page.inner_text("#order-verify")

        nav("analytics")
        page.wait_for_timeout(500)
        page.get_by_role("button", name="Agent commerce").click()
        mark("analytics")
        page.wait_for_timeout(8000)
        mark("end")
        video_path = page.video.path()
        ctx.close()
        browser.close()
    shutil.copyfile(video_path, out / "owner.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")
    (out / "verify.txt").write_text(verify_text, encoding="utf-8")
    print(verify_text)


if __name__ == "__main__":
    main()
