"""Client A: Compliant AI crawler that follows website policies and receives a deposit refund."""

from __future__ import annotations

import sys
import time
from typing import Optional

import httpx

from client.common import (
    build_arg_parser,
    execute_deposit_and_policy_flow,
    print_banner,
    print_final_summary,
    print_step,
    resolve_crawler_id,
)


def run_compliant_crawler(
    *,
    base_url: str = "http://127.0.0.1:8000",
    crawler_id: Optional[str] = None,
    max_requests: Optional[int] = None,
    payment_mode: str = "auto",
    non_interactive: bool = False,
) -> int:
    resolved_id = resolve_crawler_id(crawler_id, "crawler-compliant")
    print_banner(f"CrawlerTrust Demo -- Client A: Compliant Crawler ({resolved_id})")

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0) as client:
        # Step 1: Request protected news content without authentication
        print_step(1, "Requesting protected /gateway/news without credentials")
        initial_resp = client.get(
            "/gateway/news",
            headers={"X-Crawler-Id": resolved_id},
        )
        print(f"  -> HTTP Status: {initial_resp.status_code}")
        if initial_resp.status_code != 402:
            print(f"  [ERROR] Expected HTTP 402 Payment Required, got {initial_resp.status_code}")
            return 1

        challenge = initial_resp.json()
        print(f"  -> Challenge ID   : {challenge['challenge_id']}")
        print(f"  -> Required Stake : ${challenge['deposit']['amount']} {challenge['deposit']['currency']}")
        print(f"  -> Policy Version : {challenge['policy_version']}")
        print(f"  -> Notice         : {challenge['notice']}")

        # Step 2: Fetch and display published crawling policies & robots.txt
        print_step(2, "Reading published /policies and /robots.txt")
        policy_resp = client.get(challenge.get("policy_url", "/policies"))
        policy_resp.raise_for_status()
        policy = policy_resp.json()

        robots_resp = client.get("/robots.txt")
        robots_resp.raise_for_status()

        allowed_paths: list[str] = policy["allowed_paths"]
        disallowed_paths: list[str] = policy["disallowed_paths"]
        rate_limit_reqs = int(policy["rate_limit_requests"])
        rate_limit_win = float(policy["rate_limit_window_seconds"])
        session_budget = int(policy["max_requests_per_session"])

        print(f"  -> Site Name          : {policy['site_name']} ({policy['site_id']})")
        print(f"  -> Allowed Paths      : {allowed_paths}")
        print(f"  -> Disallowed Paths   : {disallowed_paths}")
        print(f"  -> Rate Limit         : {rate_limit_reqs} requests / {rate_limit_win:g}s window")
        print(f"  -> Max Session Budget : {session_budget} requests")

        # Step 3: Complete deposit, server-side verification, and policy acceptance
        print_step(3, "Initiating refundable deposit, verifying capture, and accepting policy")
        verified_deposit, session_issue = execute_deposit_and_policy_flow(
            client,
            challenge=challenge,
            crawler_id=resolved_id,
            expected_payment_mode=payment_mode,
            non_interactive=non_interactive,
        )
        bearer_token = session_issue["access_token"]
        session_id = session_issue["session_id"]
        print(f"  -> Session Issued     : {session_id}")
        print(f"  -> Policy Accepted At : {session_issue['policy_accepted_at']}")
        print(f"  -> Credential Type    : Bearer <high-entropy token received; stored as SHA-256 hash on server>")

        # Step 4: Crawl only allowed paths while respecting rate limits and budget
        target_count = min(
            max_requests if max_requests is not None else min(len(allowed_paths), rate_limit_reqs),
            session_budget,
        )
        paths_to_crawl = [
            allowed_paths[i % len(allowed_paths)] for i in range(target_count)
        ]

        print_step(
            4,
            f"Crawling {len(paths_to_crawl)} allowed path(s) within rate limit ({rate_limit_reqs}/{rate_limit_win:g}s)",
        )
        auth_headers = {"Authorization": f"Bearer {bearer_token}"}
        window_counter = 0
        window_start = time.monotonic()

        for idx, path in enumerate(paths_to_crawl, start=1):
            if window_counter >= rate_limit_reqs:
                elapsed = time.monotonic() - window_start
                sleep_needed = max(0.0, (rate_limit_win - elapsed) + 0.25)
                if sleep_needed > 0:
                    print(
                        f"  -> Respecting rate limit window: sleeping {sleep_needed:.2f}s before next request..."
                    )
                    time.sleep(sleep_needed)
                window_counter = 0
                window_start = time.monotonic()

            gateway_url = "/gateway" if path == "/" else f"/gateway{path}"
            resp = client.get(gateway_url, headers=auth_headers)
            window_counter += 1
            rem_budget = resp.headers.get("X-CrawlerTrust-Remaining-Budget", "?")
            rem_win = resp.headers.get("X-CrawlerTrust-Remaining-Window", "?")
            print(
                f"  [{idx}/{len(paths_to_crawl)}] GET {gateway_url:<26} -> HTTP {resp.status_code} "
                f"(len={len(resp.text)} bytes, rem_budget={rem_budget}, rem_window={rem_win})"
            )
            if resp.status_code != 200:
                print(f"  [ERROR] Compliant request unexpectedly failed: {resp.text}")
                return 1

        # Step 5: Complete session and request deposit refund evaluation
        print_step(5, "Completing session and requesting deposit refund decision")
        complete_resp = client.post("/sessions/complete", headers=auth_headers)
        if complete_resp.status_code != 200:
            print(
                f"  [ERROR] Session completion failed (HTTP {complete_resp.status_code}): {complete_resp.text}"
            )
            return 1

        complete_data = complete_resp.json()
        final_session = complete_data["session"]
        final_deposit = complete_data["deposit"]

        try:
            deposit_resp = client.get(
                f"/deposits/{final_deposit['deposit_id']}",
                headers=auth_headers,
            )
            deposit_resp.raise_for_status()
            refreshed_deposit = deposit_resp.json()
            if (
                not isinstance(refreshed_deposit, dict)
                or refreshed_deposit.get("deposit_id") != final_deposit["deposit_id"]
                or not {
                    "payment_mode",
                    "simulated",
                    "deposit_state",
                    "payment_status",
                    "policy_decision",
                    "refund_status",
                }.issubset(refreshed_deposit)
            ):
                raise ValueError("Deposit status response is incomplete or mismatched.")
            final_deposit = refreshed_deposit
        except (httpx.HTTPError, ValueError) as exc:
            print(
                "  [WARNING] Could not refresh the latest deposit state "
                f"({type(exc).__name__}); displaying the session-completion response."
            )

        # Step 6: Fetch session-scoped audit summary and display outcome
        print_step(6, "Final Deposit Accountability & Audit Summary")
        summary_resp = client.get("/audit/summary", headers=auth_headers)
        summary_resp.raise_for_status()
        audit_summary = summary_resp.json()

        print_final_summary(final_deposit, final_session, audit_summary)

        if (
            final_deposit["deposit_state"] == "REFUNDED"
            and final_deposit["policy_decision"] == "ELIGIBLE_FOR_REFUND"
            and final_deposit["refund_status"] == "REFUNDED"
        ):
            print("\n[SUCCESS] Compliant crawler completed crawl and received full deposit refund.")
            return 0

        print(
            f"\n[ERROR] Expected deposit_state=REFUNDED, got {final_deposit['deposit_state']}"
        )
        return 1


def main() -> int:
    parser = build_arg_parser(
        description="Run the CrawlerTrust compliant crawler demonstration (Client A).",
        default_prefix="crawler-compliant",
    )
    args = parser.parse_args()
    try:
        return run_compliant_crawler(
            base_url=args.base_url,
            crawler_id=args.crawler_id,
            max_requests=args.max_requests,
            payment_mode=args.payment_mode,
            non_interactive=args.non_interactive,
        )
    except Exception as exc:
        print(f"\n[FATAL] Compliant crawler error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
