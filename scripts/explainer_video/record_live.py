"""Record the owner's end-to-end story on the live gateway, in a visible browser.

The browser pauses wherever a person has to sign in (SARA, Log in with PayPal, approving a
deposit at PayPal); the recorder never types a password. Everything else is clicked here.

Order: home -> sign in -> Overview -> Setup (add website, connect PayPal, deposit and rules)
-> Playground: Price scraper (deposit kept) -> Polite researcher (hold released)
-> Deposits (inspector, Verify with PayPal) -> Earnings -> Overview.

Usage: python scripts/explainer_video/record_live.py --base https://sara-gateway.onrender.com
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

W, H = 1536, 864
WAIT_PERSON = 15 * 60 * 1000  # a person signing in


PAYPAL_WEB = "https://www.sandbox.paypal.com"


def record_paypal_activity(browser, out: Path, account: str, say) -> str | None:
    """Record a Sandbox PayPal account's activity: the person signs in, then opens the SARA payment."""
    ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(out / f"raw_{account}"),
                              record_video_size={"width": W, "height": H}, locale="en-US")
    page = ctx.new_page()
    t0 = time.monotonic()
    page.goto(f"{PAYPAL_WEB}/signin", wait_until="domcontentloaded")
    print(f"{time.monotonic() - t0:8.2f}s  {account}_signin", flush=True)
    say(f"sign in to PayPal with the Sandbox {account} account")
    page.wait_for_url(lambda url: "/signin" not in url and "/authflow" not in url and "/auth/" not in url, timeout=WAIT_PERSON)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)
    print(f"{time.monotonic() - t0:8.2f}s  {account}_signed_in", flush=True)
    page.goto(f"{PAYPAL_WEB}/myaccount/activities/", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    print(f"{time.monotonic() - t0:8.2f}s  {account}_activity", flush=True)
    say(f"in the {account} account's activity, open the latest SARA $5.00 payment, then close that tab")
    page.wait_for_event("close", timeout=WAIT_PERSON)
    print(f"{time.monotonic() - t0:8.2f}s  {account}_closed", flush=True)
    path = page.video.path() if page.video else None
    ctx.close()
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://sara-gateway.onrender.com")
    ap.add_argument("--site", default="https://sartechlabs.com")
    ap.add_argument("--deposit", default="5")
    ap.add_argument("--out", default="video-out/rec_live")
    ap.add_argument("--window", default="", help="window position x,y, for example 1940,0 for a second screen")
    ap.add_argument("--skip-paypal-proof", action="store_true")
    ap.add_argument("--only-paypal-proof", action="store_true", help="record only the PayPal account activity")
    ap.add_argument("--headless", action="store_true", help="for a dry run against a local mock gateway")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    marks: dict[str, float] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=[f"--window-size={W + 16},{H + 140}"] + ([f"--window-position={args.window}"] if args.window else []))
        if args.only_paypal_proof:
            for account in ("business", "personal"):
                path = record_paypal_activity(browser, out, account, lambda m: print(f"WAITING FOR YOU: {m}", flush=True))
                if path:
                    shutil.copyfile(path, out / f"paypal_{account}.webm")
            browser.close()
            return
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(out / "raw"),
                                  record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        popups: list[Page] = []
        page.on("popup", lambda p: (popups.append(p), mark(f"popup_{len(popups)}")))
        t0 = time.monotonic()

        def mark(name: str) -> None:
            marks[name] = round(time.monotonic() - t0, 2)
            print(f"{marks[name]:8.2f}s  {name}", flush=True)

        def say(msg: str) -> None:
            print(f"WAITING FOR YOU: {msg}", flush=True)

        def nav(view: str) -> None:
            link = page.locator(f".sidenav-link[data-view='{view}']")
            if not link.is_visible():
                for toggle in page.locator(".sidenav-parent").all():
                    toggle.click()
                    page.wait_for_timeout(250)
                    if link.is_visible():
                        break
            link.click()
            page.wait_for_timeout(1200)
            page.evaluate("scrollTo(0, 0)")

        def glide(selector: str, block: str = "center") -> None:
            page.evaluate(f"document.querySelector({json.dumps(selector)})?.scrollIntoView({{block: '{block}', behavior: 'smooth'}})")
            page.wait_for_timeout(900)

        # 1. Home and sign-in (the person types the password).
        page.goto(base + "/", wait_until="networkidle")
        mark("home")
        page.wait_for_timeout(5000)
        if not page.locator("[data-open-signin]").count():  # no sign-in configured (dry run)
            page.goto(base + "/console", wait_until="networkidle")
        if "/console" not in page.url:
            page.locator("[data-open-signin]").first.click()
            mark("signin_open")
            say("sign in to SARA in the recording window")
            page.wait_for_url("**/console**", timeout=WAIT_PERSON)
        page.wait_for_load_state("networkidle")
        mark("overview")
        page.wait_for_timeout(4000)

        # 2. Setup: add the website.
        nav("setup")
        mark("setup")
        page.wait_for_timeout(1500)
        host = args.site.split("//", 1)[-1].strip("/")
        if not page.locator("#site-list", has_text=host).count():
            if page.locator("#site-add-open").is_visible():
                page.locator("#site-add-open").click()
                page.wait_for_timeout(600)
            page.locator("#site-add-input").click()
            page.locator("#site-add-input").press_sequentially(args.site, delay=70)
            page.wait_for_timeout(400)
            mark("site_typed")
            page.locator("#site-add-button").click()
            page.wait_for_selector(f"#site-list :text('{host.removeprefix('www.')}')", timeout=60000)
            page.wait_for_load_state("networkidle")
            mark("site_added")
            page.wait_for_timeout(3000)
        glide("#site-settings-card", "start")
        mark("rules")
        page.wait_for_timeout(3500)

        # 3. Connect PayPal (the person signs in at PayPal).
        glide(".payouts-block")
        login = page.locator(".payouts-block [data-paypal-login]")
        if login.is_visible():
            mark("paypal_login_click")
            login.click()
            say("sign in at PayPal with the Sandbox business account and agree")
            page.wait_for_url("**paypal=connected**", timeout=WAIT_PERSON)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            glide(".payouts-block")
            mark("paypal_connected")
            page.wait_for_timeout(3500)
        else:
            mark("paypal_already_connected")
            page.wait_for_timeout(2500)

        # 4. Deposit and blocked paths.
        glide("#cfg-deposit-read")
        if page.locator("#cfg-deposit-edit").is_visible():
            page.locator("#cfg-deposit-edit").click()
            page.wait_for_timeout(400)
            page.locator("#cfg-deposit").fill("")
            page.locator("#cfg-deposit").press_sequentially(args.deposit, delay=120)
            page.locator("#cfg-deposit-save").click()
            page.wait_for_timeout(1500)
            mark("deposit_saved")
        glide("#disallowed-add-form")
        for path in ("/admin", "/private"):
            listed = page.evaluate(
                "(p) => [...document.querySelectorAll('#disallowed-add-form ~ *, .rule-list li, .path-chip')].some(e => e.textContent.trim().startsWith(p))",
                path,
            )
            if not listed:
                page.locator("#disallowed-add-input").click()
                page.locator("#disallowed-add-input").press_sequentially(path, delay=90)
                page.locator("#disallowed-add-form button[type=submit]").click()
                page.wait_for_timeout(1200)
        mark("rules_saved")
        page.wait_for_timeout(2500)

        # 5. Playground runs (the person approves each deposit at PayPal).
        def run_agent(title: str, key: str) -> None:
            nav("playground")
            page.wait_for_timeout(1200)
            page.locator(".lab-scenario", has_text=title).first.click()
            page.wait_for_timeout(800)
            mark(f"{key}_selected")
            page.locator("#lab-run").click()
            mark(f"{key}_run")
            # The previous run's outcome is cleared first; then wait for PayPal's button or a result.
            try:
                page.locator("#lab-outcome").wait_for(state="hidden", timeout=15000)
            except Exception:
                pass
            state = "?"
            for _ in range(600):
                if page.locator("#lab-outcome").is_visible():
                    state = "outcome"
                elif page.locator("#lab-paypal-slot paypal-button").count() or page.locator("#lab-approve").is_visible():
                    state = "approval"
                if state != "?":
                    break
                page.wait_for_timeout(500)
            if state == "outcome":  # simulated payments: no approval step
                mark(f"{key}_outcome")
                glide("#lab-outcome", "start")
                page.wait_for_timeout(7000)
                mark(f"{key}_outcome_read")
                return
            glide("#lab-approval")
            page.wait_for_timeout(1500)
            mark(f"{key}_paypal_button")
            button = page.locator("#lab-paypal-slot paypal-button")
            if button.count():
                button.first.click()
            else:
                page.locator("#lab-approve").click()
            say(f"approve the {title} deposit at PayPal with the Sandbox personal account")
            page.locator("#lab-approval").wait_for(state="hidden", timeout=WAIT_PERSON)
            mark(f"{key}_approved")
            page.locator("#lab-outcome").wait_for(state="visible", timeout=15 * 60 * 1000)
            page.wait_for_timeout(1500)
            mark(f"{key}_outcome")
            glide("#lab-outcome", "start")
            page.wait_for_timeout(7000)
            mark(f"{key}_outcome_read")
            print("TIMELINE:", page.locator("#lab-timeline").inner_text()[:800].replace(chr(10), " | "), flush=True)

        run_agent("Price scraper", "scraper")
        print("OUTCOME:", page.locator("#lab-outcome").inner_text()[:600].replace(chr(10), " | "), flush=True)
        ids = page.locator("#lab-outcome .lab-outcome-actions .muted")
        scraper_deposit = ids.first.inner_text().replace("Deposit ", "").strip() if ids.count() else ""
        run_agent("Polite researcher", "polite")

        # 6. Money: the kept deposit, Verify with PayPal, earnings, overview.
        nav("deposits")
        mark("deposits")
        page.wait_for_timeout(3500)
        row = page.locator("#grid-deposits .ag-row", has_text=scraper_deposit[-8:]).first if scraper_deposit else page.locator("#grid-deposits .ag-row").first
        if not row.count():
            row = page.locator("#grid-deposits .ag-row").first
        row.click()
        mark("deposit_opened")
        page.wait_for_timeout(4000)
        verify = page.locator("#paypal-panel button", has_text="Verify deposit with PayPal")
        glide("#explain-card", "start")
        verify_text = ""
        if verify.is_enabled():
            verify.click()
            mark("verify_clicked")
            page.locator("#paypal-panel table").wait_for(timeout=180000)
            page.wait_for_timeout(800)
            glide("#paypal-panel table")
            mark("verify_result")
            page.wait_for_timeout(5000)
            verify_text = page.locator("#paypal-panel").inner_text()

        nav("commerce")
        mark("earnings")
        page.wait_for_timeout(5000)
        nav("overview")
        mark("overview_end")
        page.wait_for_timeout(6000)
        mark("end")

        video_path = page.video.path()
        popup_videos = [p.video.path() for p in popups if p.video]
        ctx.close()

        # 7. Proof in PayPal itself: each Sandbox account's own activity (the person signs in).
        proof_videos = {}
        for account in ([] if args.skip_paypal_proof else ["business", "personal"]):
            proof_videos[account] = record_paypal_activity(browser, out, account, say)
        browser.close()

    shutil.copyfile(video_path, out / "live.webm")
    for i, path in enumerate(popup_videos, 1):
        shutil.copyfile(path, out / f"popup_{i}.webm")
    for account, path in proof_videos.items():
        if path:
            shutil.copyfile(path, out / f"paypal_{account}.webm")
    (out / "marks.json").write_text(json.dumps(marks, indent=2), encoding="utf-8")
    (out / "verify.txt").write_text(verify_text, encoding="utf-8")
    print("saved", out)


if __name__ == "__main__":
    main()
