"""Record the demo running in a real Chrome window on this Windows PC (screen capture).

Devpost asks for the project shown working on the device it was built for. This recorder opens
the installed Google Chrome on one monitor and captures that monitor with ffmpeg (gdigrab), so the
clip shows the real browser: its tab strip, address bar (localhost) and window. A script does the
clicking and typing so every take is the same; a small cursor marks where it clicks.

The take goes through the four demo tabs:
  Website           Sentinel News as a person sees it: the shop with PayPal buttons, the notice for agents
  Agent             a person asks Aria for something; it pays a deposit, buys, asks before bigger buys
                    (one approved, the subscription declined); then the rule-breaking scraper
  Owner dashboard   overview with the kept deposit's AI explanation, security, operations, commerce,
                    Verify with PayPal (MCP), AG Studio
  About this demo   a glance

Writes <out>/device.mp4 and <out>/marks.json (seconds into device.mp4, synced by a flash frame).

Usage (with a Sandbox gateway on 8011 and nothing else on that monitor):
  python scripts/explainer_video/record_device.py --base http://127.0.0.1:8011 --out video-out/rec_device
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx
import imageio_ffmpeg
import numpy as np
from playwright.sync_api import sync_playwright

FF = imageio_ffmpeg.get_ffmpeg_exe()
TASK = "Catch me up on today's news from Sentinel News, and get me whatever they have on how AI agents pay websites: videos, guides, anything useful."

# A visible pointer (the OS cursor stays with the person using the other monitor).
CURSOR_JS = r"""
(() => {
  const add = () => {
    if (document.getElementById('__rec_cursor')) return;
    const c = document.createElement('div');
    c.id = '__rec_cursor';
    c.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2l15 9-6.5 1.6L9.6 19z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position: 'fixed', left: '0', top: '0', zIndex: 2147483647, pointerEvents: 'none',
      transform: `translate(${sessionStorage.__cx || innerWidth * 0.55}px, ${sessionStorage.__cy || innerHeight * 0.6}px)`,
      transition: 'transform 0.55s cubic-bezier(.3,.7,.3,1)', filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.5))'});
    document.documentElement.append(c);
  };
  window.__recMove = (x, y) => { add(); sessionStorage.__cx = x; sessionStorage.__cy = y;
    document.getElementById('__rec_cursor').style.transform = `translate(${x}px, ${y}px)`; };
  window.__recClick = () => { const c = document.getElementById('__rec_cursor'); if (!c) return;
    c.animate([{scale: 1}, {scale: 0.82}, {scale: 1}], {duration: 260}); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
})();
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8011")
    ap.add_argument("--out", default="video-out/rec_device")
    ap.add_argument("--x", type=int, default=-1920, help="left of the monitor's work area")
    ap.add_argument("--y", type=int, default=-47, help="top of the monitor's work area")
    ap.add_argument("--w", type=int, default=1920)
    ap.add_argument("--h", type=int, default=1032, help="work area height (without the taskbar)")
    ap.add_argument("--scale", default="1.25", help="Chrome's device scale factor, for legible text")
    ap.add_argument("--decline", default="subscription")
    ap.add_argument("--skip-agent", action="store_true", help="skip the AI assistant run (for a quick test)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api = httpx.Client(base_url=args.base, timeout=30)
    marks: dict[str, float] = {}
    video = out / "device.mp4"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=False,
            # Chrome takes window positions in scaled pixels: start small on the monitor, then maximize there.
            args=[f"--window-position={int(args.x / float(args.scale)) + 300},{int(args.y / float(args.scale)) + 100}", "--window-size=1100,700",
                  f"--force-device-scale-factor={args.scale}", "--no-first-run", "--no-default-browser-check",
                  "--hide-crash-restore-bubble", "--disable-features=Translate,PasswordManagerOnboarding"],
            ignore_default_args=["--enable-automation"],
        )
        ctx = browser.new_context(no_viewport=True, color_scheme="dark")
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "maximized"}})
        page.goto(f"{args.base}/website", wait_until="networkidle")
        page.bring_to_front()
        page.wait_for_timeout(2500)

        ff = subprocess.Popen(
            [FF, "-y", "-v", "error", "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0",
             "-offset_x", str(args.x), "-offset_y", str(args.y), "-video_size", f"{args.w}x{args.h}", "-i", "desktop",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", str(video)],
            stdin=subprocess.PIPE,
        )
        t0 = time.monotonic()

        def mark(name: str) -> None:
            marks[name] = round(time.monotonic() - t0, 2)
            print(f"{marks[name]:7.2f}s  {name}", flush=True)

        def wait(ms: int) -> None:
            page.wait_for_timeout(ms)

        def point(target, click: bool = True) -> None:
            loc = page.locator(target) if isinstance(target, str) else target
            loc.first.scroll_into_view_if_needed()
            box = loc.first.bounding_box()
            if box:
                page.evaluate("([x, y]) => window.__recMove && window.__recMove(x, y)", [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
                wait(650)
            if click:
                page.evaluate("() => window.__recClick && window.__recClick()")
                loc.first.click()

        def scroll_to(y: int, ms: int = 1100) -> None:
            page.evaluate(
                """([y, ms]) => new Promise(r => { const s = scrollY, d = y - s, t0 = performance.now();
                   const f = (t) => { const k = Math.min(1, (t - t0) / ms); scrollTo(0, s + d * (1 - Math.cos(Math.PI * k)) / 2);
                   k < 1 ? requestAnimationFrame(f) : r(); }; requestAnimationFrame(f); })""",
                [y, ms],
            )

        def top_of(sel: str, offset: int = 90) -> int:
            return int(page.evaluate(f"document.querySelector({json.dumps(sel)}).getBoundingClientRect().top + scrollY - {offset}"))

        def pane(selector: str, block: str = "center") -> None:
            page.evaluate("([s, b]) => { const n = document.querySelector(s); if (n) n.scrollIntoView({block: b, behavior: 'smooth'}); }", [selector, block])

        def tab(name: str) -> None:
            point(page.locator("nav.nav a", has_text=name))
            page.wait_for_load_state("networkidle")
            wait(900)

        # Sync: a one-frame-group flash so marks line up with the recording exactly.
        wait(1500)
        page.evaluate("""() => { const f = document.createElement('div'); f.id = '__flash';
            Object.assign(f.style, {position: 'fixed', inset: 0, background: '#ff00ff', zIndex: 2147483647});
            document.documentElement.append(f); setTimeout(() => f.remove(), 500); }""")
        mark("flash")
        wait(1500)

        # --- Website ---------------------------------------------------------------------------
        mark("website")
        wait(3000)
        scroll_to(top_of("#shop"), 1400)
        mark("shop")
        wait(4000)
        scroll_to(top_of("#subscribe", 60), 1100)
        mark("pro")
        wait(2500)
        scroll_to(top_of("#for-agents", 120), 1000)
        mark("for_agents")
        wait(4500)
        scroll_to(0, 900)
        wait(600)

        # --- Agent: a person's assistant ----------------------------------------------------------
        tab("Agent")
        mark("agent")
        wait(2500)
        if not args.skip_agent:
            point("#task")
            page.keyboard.type(TASK, delay=26)
            mark("task_filled")
            wait(900)
            page.keyboard.press("Enter")
            mark("run_started")
            run_id = None
            for _ in range(60):
                run_id = api.get("/demo/config").json().get("active_run_id")
                if run_id:
                    break
                time.sleep(0.2)
            seen = 0
            opened = False
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                snap = api.get(f"/demo/runs/{run_id}?after={seen}").json()
                for e in snap["events"]:
                    d = e["data"]
                    if e["kind"] == "tool_call":
                        n = len([k for k in marks if k.startswith(f"call:{d['name']}:")])
                        mark(f"call:{d['name']}:{n}")
                    elif e["kind"] == "purchase_review":
                        mark(f"review:{d.get('verdict')}:{d.get('title', '')[:40]}")
                seen = snap["next_seq"]
                if not opened and any(k.startswith("call:fetch_page") for k in marks):
                    point("#details-toggle")
                    mark("details_opened")
                    opened = True
                status = snap["status"]
                if status == "awaiting_decision":
                    pending = snap["pending_decision"] or {}
                    approve = pending.get("payment_type") != args.decline
                    page.wait_for_selector("#approve-button", timeout=15000)
                    pane('.stage[data-stage="buy"]')
                    mark(f"decision_shown:{pending.get('payment_type')}")
                    page.locator("#approve-button").scroll_into_view_if_needed()
                    point("#approve-button" if approve else "#decline-button", click=False)
                    wait(2800)
                    point("#approve-button" if approve else "#decline-button")
                    mark(f"decision_{'approved' if approve else 'declined'}:{pending.get('payment_type')}")
                    wait(1500)
                elif status in ("completed", "failed", "cancelled"):
                    break
                else:
                    pane('.stage[data-state="active"]')
                time.sleep(0.6)
            mark("run_finished")
            wait(1800)
            pane(".msg.ai .answer", "start")
            mark("answer")
            wait(5000)
            pane(".wallet", "start")
            mark("wallet")
            wait(4000)

        # --- Agent: the rule-breaking scraper ------------------------------------------------------
        point("#new-chat")
        wait(1500)
        if not page.locator("#split.details-open").count():
            point("#details-toggle")
        mark("scraper_ready")
        wait(1200)
        point(".suggestion.rogue-pick")
        mark("scraper_started")
        rogue = None
        for _ in range(60):
            rogue = api.get("/demo/config").json().get("active_run_id")
            if rogue:
                break
            time.sleep(0.2)
        while api.get(f"/demo/runs/{rogue}").json()["status"] not in ("completed", "failed", "cancelled"):
            pane('.stage[data-state="active"]')
            time.sleep(0.5)
        page.wait_for_selector(".msg.sara", timeout=90000)
        mark("scraper_finished")
        pane(".msg.sara", "center")
        wait(6000)
        mark("scraper_verdict")
        wait(1500)

        # --- Owner dashboard ---------------------------------------------------------------------------
        tab("Owner dashboard")
        mark("overview")
        wait(3000)
        point(page.locator(".ag-row", has_text="Forfeited"))
        wait(3500)
        mark("overview_explained")

        def view(name: str) -> None:
            point(f".sidenav-link[data-view='{name}']")
            wait(1400)
            page.evaluate("scrollTo(0, 0)")

        view("security")
        mark("security")
        wait(4500)
        view("operations")
        mark("operations")
        wait(3500)
        view("commerce")
        mark("commerce")
        wait(4000)
        scroll_to(top_of("#grid-orders", 150), 1100)
        mark("orders")
        wait(1500)
        row = page.locator("#grid-orders .ag-row", has_text="Playbook").first
        idx = row.get_attribute("row-index") if row.count() else None
        btn = page.locator(f"#grid-orders .ag-row[row-index='{idx}'] button", has_text="Verify") if idx else page.locator("#grid-orders button", has_text="Verify")
        point(btn)
        mark("verify_clicked")
        page.wait_for_selector("#order-verify table", timeout=120000)
        mark("verify_result")
        pane("#order-verify", "center")
        wait(5500)
        view("analytics")
        wait(400)
        point(page.get_by_role("button", name="Agent commerce"))
        mark("analytics")
        wait(7000)

        # --- About ---------------------------------------------------------------------------------------
        tab("About this demo")
        mark("about")
        wait(4000)
        mark("end")
        wait(500)
        ff.communicate(b"q", timeout=60)
        ctx.close()
        browser.close()

    # Find the flash in the recording and shift every mark onto the video's clock.
    flash_at = find_flash(video)
    shift = flash_at - marks["flash"]
    synced = {k: round(v + shift, 2) for k, v in marks.items()}
    (out / "marks.json").write_text(json.dumps(synced, indent=2), encoding="utf-8")
    print(f"flash at {flash_at:.2f}s in the video (shift {shift:+.2f}s); saved {video}")


def find_flash(video: Path) -> float:
    """Seconds into the video of the first mostly-magenta frame."""
    w, h, fps = 64, 36, 30
    proc = subprocess.run([FF, "-v", "error", "-i", str(video), "-t", "20", "-vf", f"scale={w}:{h},fps={fps}",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    frames = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(-1, h, w, 3).astype(int)
    for i, f in enumerate(frames):
        magenta = (f[..., 0] > 200) & (f[..., 1] < 70) & (f[..., 2] > 200)
        if magenta.mean() > 0.5:
            return i / fps
    raise RuntimeError("sync flash not found in the recording")


if __name__ == "__main__":
    main()
