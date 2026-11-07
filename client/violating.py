"""Client B: Violating AI crawler that breaks path and rate-limit policies and forfeits its deposit."""

from __future__ import annotations

import sys
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


def run_violating_crawler(
    *,
    base_url: str = "http://127.0.0.1:8000",
    crawler_id: Optional[str] = None,
    max_requests: Optional[int] = None,
    payment_mode: str = "auto",
    non_interactive: bool = False,
) -> int:
    resolved_id = resolve_crawler_id(crawler_id, "crawler-violating")
    print_banner(f"SARA Demo -- Client B: Violating Crawler ({resolved_id})")

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=20.0) as client:
        # Step 1: Request protected content without credentials -> receive 402 challenge
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

        # Step 2: Complete deposit, verification, and policy acceptance
        print_step(2, "Completing deposit verification and accepting crawling policy")
        verified_deposit, session_issue = execute_deposit_and_policy_flow(
            client,
            challenge=challenge,
            crawler_id=resolved_id,
            expected_payment_mode=payment_mode,
            non_interactive=non_interactive,
        )
        deposit_id = verified_deposit["deposit_id"]
        bearer_token = session_issue["access_token"]
        auth_headers = {"Authorization": f"Bearer {bearer_token}"}
        print(f"  -> Session Issued : {session_issue['session_id']}")

        # Step 3: Access an allowed path first
        print_step(3, "Accessing allowed path (/gateway/news)")
        ok_resp = client.get("/gateway/news", headers=auth_headers)
        print(
            f"  -> GET /gateway/news                  -> HTTP {ok_resp.status_code} "
            f"(Allowed content served, {len(ok_resp.text)} bytes)"
        )
        if ok_resp.status_code != 200:
            print(f"  [ERROR] Expected HTTP 200 on allowed path, got {ok_resp.status_code}")
            return 1

        # Step 4: Deliberately attempt to access disallowed paths and encoded traversal
        print_step(4, "Attempting prohibited access to disallowed paths (/admin, /private/customer-data) & traversal")
        prohibited_targets = [
            "/gateway/admin",
            "/gateway/private/customer-data",
            "/gateway/%2e%2e/admin",
        ]
        for target in prohibited_targets:
            bad_resp = client.get(target, headers=auth_headers)
            body = bad_resp.json()
            print(
                f"  -> GET {target:<30} -> HTTP {bad_resp.status_code} | "
                f"reason_code={body.get('reason_code')} | "
                f"violation={body.get('violation')} | "
                f"disqualifying={body.get('disqualifying')}"
            )
            if bad_resp.status_code != 403:
                print(f"  [ERROR] Expected HTTP 403 on prohibited path {target}, got {bad_resp.status_code}")
                return 1

        # Step 5: Rapid-fire requests to exceed the sliding-window rate limit
        burst_count = max_requests if max_requests is not None else 5
        print_step(5, f"Sending rapid burst of {burst_count} requests to exceed rate limit")
        rate_limited_seen = 0
        for idx in range(1, burst_count + 1):
            burst_resp = client.get("/gateway/news/technology", headers=auth_headers)
            if burst_resp.status_code == 429:
                rate_limited_seen += 1
                err_body = burst_resp.json()
                print(
                    f"  [Burst {idx}/{burst_count}] GET /gateway/news/technology -> HTTP 429 | "
                    f"reason_code={err_body.get('reason_code')} | "
                    f"session_disqualified={err_body.get('session_disqualified')}"
                )
            else:
                print(
                    f"  [Burst {idx}/{burst_count}] GET /gateway/news/technology -> HTTP {burst_resp.status_code}"
                )

        if rate_limited_seen == 0:
            print("  [ERROR] Expected rate limit (HTTP 429) to trigger during burst.")
            return 1

        # Step 6: Complete session and attempt to claim a deposit refund
        print_step(6, "Completing session and attempting to claim deposit refund")
        complete_resp = client.post("/sessions/complete", headers=auth_headers)
        if complete_resp.status_code != 200:
            print(
                f"  [ERROR] Session completion endpoint returned HTTP {complete_resp.status_code}: {complete_resp.text}"
            )
            return 1

        complete_data = complete_resp.json()
        print(f"  -> Refund Eligible  : {complete_data['refund_eligible']}")
        print(f"  -> Decision Reason  : {complete_data['decision_reason']}")

        # Also attempt an explicit direct refund call on /deposits/{deposit_id}/refund
        refund_attempt = client.post(f"/deposits/{deposit_id}/refund", headers=auth_headers)
        refund_body = refund_attempt.json()
        print(
            f"  -> Explicit POST /deposits/{deposit_id}/refund -> "
            f"deposit_state={refund_body.get('deposit_state')}, "
            f"policy_decision={refund_body.get('policy_decision')}, "
            f"refund_status={refund_body.get('refund_status')}"
        )

        # Step 7: Attempt to reuse the completed session token on the gateway
        print_step(7, "Attempting to reuse completed session credential on /gateway/news")
        reuse_resp = client.get("/gateway/news", headers=auth_headers)
        reuse_body = reuse_resp.json()
        print(
            f"  -> GET /gateway/news (post-completion) -> HTTP {reuse_resp.status_code} | "
            f"reason_code={reuse_body.get('reason_code')}"
        )

        # Step 8: Display final session status, deposit decision, and audit summary
        print_step(8, "Final Deposit Accountability & Audit Summary")
        me_resp = client.get("/sessions/me", headers=auth_headers)
        me_resp.raise_for_status()
        final_session = me_resp.json()

        summary_resp = client.get("/audit/summary", headers=auth_headers)
        summary_resp.raise_for_status()
        audit_summary = summary_resp.json()

        print_final_summary(refund_body, final_session, audit_summary)

        if (
            refund_body.get("deposit_state") == "FORFEITED"
            and refund_body.get("policy_decision") == "FORFEITED_BY_POLICY"
            and refund_body.get("refund_status") == "NOT_APPLICABLE_FORFEITED"
            and refund_body.get("payment_status") == "CAPTURED"
        ):
            print(
                "\n[SUCCESS] Violating crawler was blocked from disallowed content, "
                "rate-limited, and its deposit was marked FORFEITED_BY_POLICY (no refund issued)."
            )
            return 0

        print(
            f"\n[ERROR] Unexpected deposit state for violating crawler: {refund_body}"
        )
        return 1


def main() -> int:
    parser = build_arg_parser(
        description="Run the SARA violating crawler demonstration (Client B).",
        default_prefix="crawler-violating",
    )
    args = parser.parse_args()
    try:
        return run_violating_crawler(
            base_url=args.base_url,
            crawler_id=args.crawler_id,
            max_requests=args.max_requests,
            payment_mode=args.payment_mode,
            non_interactive=args.non_interactive,
        )
    except Exception as exc:
        print(f"\n[FATAL] Violating crawler error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

