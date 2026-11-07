"""Record the publisher console: Commerce view, Verify with PayPal (MCP), AG Studio dashboard."""

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
    ap.add_argument("--out", default="video-out/rec_console")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    marks: dict[str, float] = {}
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

        page.goto(f"{args.base}/console#commerce", wait_until="networkidle")
        page.wait_for_timeout(2500)
        mark("commerce")
        page.wait_for_timeout(4500)
        grid_y = page.evaluate("document.querySelector('#grid-orders').getBoundingClientRect().top + scrollY - 150")
        smooth_scroll(int(grid_y), 1100)
        mark("orders")
        page.wait_for_timeout(3000)
        # Verify the e-book order (a paid product order) against PayPal through PayPal's MCP server.
        row = page.locator("#grid-orders .ag-row", has_text="Playbook").first
        row_index = row.get_attribute("row-index")
        button = page.locator(f"#grid-orders .ag-row[row-index='{row_index}'] button", has_text="Verify").first
        if not button.count():
            button = page.locator("#grid-orders button", has_text="Verify").first
        button.click()
        mark("verify_clicked")
        page.wait_for_selector("#order-verify table", timeout=120000)
        mark("verify_result")
        page.evaluate("document.querySelector('#order-verify').scrollIntoView({block: 'center', behavior: 'smooth'})")
        page.wait_for_timeout(6500)
        verify_text = page.inner_text("#order-verify")
        page.goto(f"{args.base}/console#analytics", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.get_by_role("button", name="Agent commerce").click()
        mark("analytics")
        page.wait_for_timeout(8000)
        mark("end")
        video_path = page.video.path()
        ctx.close()
        browser.close()
    shutil.copyfile(video_path, out / "console.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")
    (out / "verify.txt").write_text(verify_text, encoding="utf-8")
    print(verify_text)


if __name__ == "__main__":
    main()
