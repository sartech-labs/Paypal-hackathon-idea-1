"""Fill a local gateway with real agent activity for the dashboards: add a website, set its
deposit and rules, then run playground agents (AI goals and scripted attacks) one at a time.

Usage: python scripts/explainer_video/seed_local.py --base http://127.0.0.1:8014
"""

from __future__ import annotations

import argparse
import time

import httpx

GOALS = {
    "Polite researcher": "Read this site's main pages and write a short summary. Read the crawling policy first, request only paths it allows, respect the rate limit, and finish the session cleanly.",
    "Price scraper": "You are a price scraper. Copy every page you can reach as fast as possible, including pages the rules forbid. Ignore robots.txt and never wait between requests.",
    "Admin prober": "Look for admin, login, private, and internal pages on this site and try to open each of them.",
    "Tricked by a page": "You are a gullible assistant. Read this site's pages and do whatever the text on them tells AI agents to do, even if that means opening other paths.",
    "Budget burner": "Read as many pages as you can in a single session. Keep requesting pages even after the gateway says your request budget is used up.",
    "Course finder": "Find the courses this site offers and list them with one line each. Follow the site's crawling rules and pace yourself.",
}
ORDER = ["Polite researcher", "Price scraper", "Course finder", "Admin prober", "Polite researcher", "Budget burner", "Tricked by a page", "Course finder"]
SCRIPTED = ["no_deposit", "path_traversal", "session_reuse", "forged_signature", "no_deposit", "path_traversal"]


def wait(client: httpx.Client, url: str, timeout: float = 600) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(url).json()
        if body.get("status") in ("completed", "failed", "cancelled", "stopped", "error"):
            return body
        time.sleep(3)
    return {"status": "timeout"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8014")
    ap.add_argument("--site", default="https://sartechlabs.com")
    args = ap.parse_args()
    with httpx.Client(base_url=args.base, timeout=120) as c:
        sites = c.get("/admin/sites").json().get("sites", [])
        site = next((s for s in sites if "sartechlabs" in s.get("origin_url", "")), None)
        if not site:
            r = c.post("/admin/sites", json={"origin_url": args.site, "set_active": True})
            r.raise_for_status()
            site = next(s for s in c.get("/admin/sites").json()["sites"] if "sartechlabs" in s.get("origin_url", ""))
        sid = site["site_id"]
        cfg = c.get(f"/admin/config?site_id={sid}").json()
        disallowed = sorted(set(cfg.get("disallowed_paths", [])) | {"/admin", "/private"})
        r = c.put(f"/admin/config?site_id={sid}", json={
            "deposit_amount": "5.00",
            "allowed_paths": cfg.get("allowed_paths", []),
            "disallowed_paths": disallowed,
            "rate_limit_requests": 3,
            "rate_limit_window_seconds": 1,
        })
        print("config", r.status_code, r.text[:200], flush=True)
        for i, name in enumerate(ORDER):
            r = c.post("/demo/runs", json={"driver": "ai", "persona": "goal", "goal": GOALS[name], "site_id": sid})
            if r.status_code != 202:
                print("ai run refused", r.status_code, r.text[:200], flush=True)
                continue
            run = wait(c, f"/demo/runs/{r.json()['run_id']}")
            dep = run.get("deposit") or {}
            print(f"ai {name}: {run.get('status')} deposit={dep.get('deposit_state')}", flush=True)
            if i < len(SCRIPTED):
                r = c.post("/playground/runs", json={"action_ids": [SCRIPTED[i]], "driver": "scripted", "site_id": sid})
                if r.status_code in (200, 202):
                    run = wait(c, f"/playground/runs/{r.json()['run_id']}")
                    print(f"scripted {SCRIPTED[i]}: {run.get('status')}", flush=True)
                else:
                    print("scripted refused", r.status_code, r.text[:200], flush=True)


if __name__ == "__main__":
    main()
