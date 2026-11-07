"""Shared HTTP helpers, CLI argument parsing, and console formatting for crawler clients."""

from __future__ import annotations

import argparse
import secrets
import sys
from typing import Any, Optional

import httpx


def build_arg_parser(description: str, default_prefix: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL of the SARA FastAPI gateway (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--crawler-id",
        default=None,
        help=f"Optional crawler identifier (default: auto-generated '{default_prefix}-<hex>')",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Optional override for number of content requests to issue",
    )
    parser.add_argument(
        "--payment-mode",
        choices=["auto", "mock", "paypal_sandbox"],
        default="auto",
        help="Expected payment mode (auto detects from server challenge/deposit response)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt for interactive browser approval in PayPal Sandbox mode",
    )
    return parser


def resolve_crawler_id(cli_value: Optional[str], prefix: str) -> str:
    if cli_value and cli_value.strip():
        return cli_value.strip()
    return f"{prefix}-{secrets.token_hex(3)}"


def print_banner(title: str) -> None:
    line = "=" * 76
    print(f"\n{line}")
    print(f"  {title}")
    print(f"{line}")


def print_step(step_num: int, title: str) -> None:
    print(f"\n[{step_num:02d}] {title}")
    print("-" * 76)


def execute_deposit_and_policy_flow(
    client: httpx.Client,
    *,
    challenge: dict[str, Any],
    crawler_id: str,
    expected_payment_mode: str = "auto",
    non_interactive: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Execute the complete deposit initiation, capture, server-side verification,
    and explicit policy acceptance workflow through the gateway API.

    Returns:
        (verified_deposit_dict, session_issue_dict)
    """
    challenge_id = challenge["challenge_id"]
    policy_version = challenge["policy_version"]

    # 1. Create pending deposit and order
    create_resp = client.post(
        "/deposits/create",
        json={"challenge_id": challenge_id, "crawler_id": crawler_id},
    )
    if create_resp.status_code != 201:
        raise RuntimeError(
            f"Deposit creation failed (HTTP {create_resp.status_code}): {create_resp.text}"
        )
    deposit = create_resp.json()
    deposit_id = deposit["deposit_id"]
    payment_mode = deposit["payment_mode"]
    simulated = deposit["simulated"]

    if expected_payment_mode not in ("auto", payment_mode):
        raise RuntimeError(
            f"Server payment_mode is '{payment_mode}', but CLI requested '--payment-mode {expected_payment_mode}'."
        )

    mode_label = (
        "SIMULATED MOCK PAYMENT (No real funds moved)"
        if simulated
        else "PAYPAL SANDBOX API v2"
    )
    print(f"  -> Deposit ID          : {deposit_id}")
    print(f"  -> Payment Mode        : {payment_mode} ({mode_label})")
    print(f"  -> Provider Order ID   : {deposit.get('provider_order_id')}")
    print(f"  -> Order Status        : {deposit.get('provider_order_status')}")
    print(f"  -> Deposit State       : {deposit.get('deposit_state')} (payment_status={deposit.get('payment_status')})")

    # If running against live PayPal Sandbox and order is CREATED (awaiting buyer approval)
    if (
        not simulated
        and deposit.get("provider_order_status") in ("CREATED", "PAYER_ACTION_REQUIRED")
        and deposit.get("approval_url")
    ):
        print("\n  [!] PayPal Sandbox Payer Approval Required:")
        print(f"      Open this URL in your browser and log in with your PayPal Sandbox buyer account:")
        print(f"      {deposit['approval_url']}")
        if not non_interactive and sys.stdin.isatty():
            input("      Press [Enter] after approving the checkout in your browser to continue...")
        else:
            print("      (Non-interactive mode enabled; attempting capture directly)")

    # 2. Capture deposit order
    cap_resp = client.post(
        f"/deposits/{deposit_id}/capture",
        json={"crawler_id": crawler_id, "challenge_id": challenge_id},
    )
    if cap_resp.status_code != 200:
        raise RuntimeError(
            f"Deposit capture failed (HTTP {cap_resp.status_code}): {cap_resp.text}"
        )
    captured_deposit = cap_resp.json()
    if captured_deposit.get("payment_status") == "AUTHORIZED":
        print(f"  -> Authorization ID    : {captured_deposit.get('provider_authorization_id')}")
        print(
            f"  -> Amount On Hold      : {captured_deposit.get('required_amount')} "
            f"{captured_deposit.get('currency')} (authorization hold; no funds captured)"
        )
    else:
        print(f"  -> Capture ID          : {captured_deposit.get('provider_capture_id')}")
        print(f"  -> Captured Amount     : {captured_deposit.get('captured_amount')} {captured_deposit.get('currency')}")

    # 3. Verify deposit server-side
    ver_resp = client.post(
        f"/deposits/{deposit_id}/verify",
        json={"crawler_id": crawler_id, "challenge_id": challenge_id},
    )
    if ver_resp.status_code != 200:
        raise RuntimeError(
            f"Server-side payment verification failed (HTTP {ver_resp.status_code}): {ver_resp.text}"
        )
    verified_deposit = ver_resp.json()
    print(
        f"  -> Server Verification : payment_status={verified_deposit['payment_status']} | "
        f"deposit_state={verified_deposit['deposit_state']}"
    )

    # 4. Explicitly accept crawling policy to obtain session bearer token
    accept_resp = client.post(
        "/sessions/accept-policy",
        json={
            "challenge_id": challenge_id,
            "deposit_id": deposit_id,
            "crawler_id": crawler_id,
            "policy_version": policy_version,
            "agree_to_terms": True,
        },
    )
    if accept_resp.status_code != 201:
        raise RuntimeError(
            f"Policy acceptance failed (HTTP {accept_resp.status_code}): {accept_resp.text}"
        )
    session_issue = accept_resp.json()
    return verified_deposit, session_issue


def print_final_summary(
    deposit: dict[str, Any],
    session: dict[str, Any],
    audit_summary: dict[str, Any],
) -> None:
    print("  [Session Accountability Metrics]")
    print(f"    Session ID              : {session['session_id']}")
    print(f"    Session Status          : {session['status']}")
    print(f"    Total Requests          : {session['total_requests']}")
    print(f"    Allowed Requests        : {session['allowed_requests']}")
    print(f"    Rejected Requests       : {session['rejected_requests']}")
    print(f"    Total Violations        : {session['total_violations']}")
    print(f"      - Disallowed Path     : {session['disallowed_path_violations']}")
    print(f"      - Rate Limit Exceeded : {session['rate_limit_violations']}")
    print(f"      - Budget Exceeded     : {session['budget_violations']}")
    print(f"    Disqualified            : {session['disqualified']}")
    if session.get("disqualification_reasons"):
        for reason in session["disqualification_reasons"]:
            print(f"      * {reason}")

    print("\n  [Deposit & Financial Outcome]")
    print(f"    Deposit ID              : {deposit['deposit_id']}")
    print(f"    Payment Mode            : {deposit['payment_mode']} (simulated={deposit['simulated']})")
    print(f"    Composite Deposit State : {deposit['deposit_state']}")
    print(f"    Payment Status          : {deposit['payment_status']}")
    print(f"    Policy Decision         : {deposit['policy_decision']}")
    print(f"    Refund Transaction      : {deposit['refund_status']}")
    if deposit.get("provider_refund_id"):
        print(f"    Provider Refund ID      : {deposit['provider_refund_id']}")
    if deposit.get("decision_reason"):
        print(f"    Decision Reason         : {deposit['decision_reason']}")

    print("\n  [Session Audit Log Summary]")
    print(f"    Audit Events Recorded   : {audit_summary['total_events']}")
    print(f"    Gateway Requests Logged : {audit_summary['total_gateway_requests']}")
    print(f"    Policy Violations Logged: {audit_summary['policy_violations']}")
    print(f"    Violation Breakdown     : {audit_summary['violation_counts_by_reason']}")

