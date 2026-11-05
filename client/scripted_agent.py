"""Scripted crawler driver: the AI agent's tool loop without a model.

It drives the same GatewayToolbox tools as the AI agent (fetch_page, read_policy,
pay_deposit, accept_policy, wait, complete_session), with fixed decisions, so the live
demo works with no AI key and gives identical gateway, PayPal, and audit behavior.
"""

from __future__ import annotations

import json
from typing import Any, Generator, Optional

from client.agent import ToolCall, Turn

# (narration, tool name, tool input); the generator receives the tool's parsed result.
Step = tuple[str, str, dict[str, Any]]
Script = Generator[Step, dict[str, Any], str]

INJECTION_MARKERS = ("note to ai agents", "ignore previous", "ignore all previous", "fetch it now")


def _has_injection(result: dict[str, Any]) -> bool:
    content = str(result.get("content") or "").lower()
    return any(marker in content for marker in INJECTION_MARKERS)


def _compliant() -> Script:
    challenge = yield ("Requesting /news to see what the site asks of AI crawlers.", "fetch_page", {"path": "/news"})
    if challenge.get("status") != 402:
        return f"Expected an HTTP 402 challenge, got {challenge.get('status')}. Stopping."
    deposit = challenge.get("deposit") or {}

    policy_result = yield (
        "The site answered HTTP 402 Payment Required. Reading its crawling policy before paying.",
        "read_policy",
        {},
    )
    policy = policy_result.get("policy") or {}
    paths = list(policy.get("allowed_paths") or ["/news"])
    # News sections first, then the rest of the allowed site (stable order otherwise).
    allowed = sorted(paths, key=lambda path: not path.startswith("/news"))
    window = float(policy.get("rate_limit_window_seconds") or 10)

    paid = yield (
        f"The deposit of {deposit.get('amount')} {deposit.get('currency')} is refundable and within the "
        "spending limit, and the policy fits the task. Paying with PayPal.",
        "pay_deposit",
        {},
    )
    if "error" in paid:
        return f"The deposit was not paid: {paid['error']}"

    accepted = yield (
        f"Deposit verified. Accepting policy {policy.get('policy_version')} to start the session.",
        "accept_policy",
        {"policy_version": str(policy.get("policy_version")), "agree_to_terms": True},
    )
    if "error" in accepted:
        return f"The policy was not accepted: {accepted['error']}"

    fetched, injection_seen, note = 0, False, ""
    budget = int(accepted.get("max_requests_per_session") or 10)
    targets = allowed[: max(1, min(4, budget - 1))]
    for index, path in enumerate(targets):
        page = yield (f"{note}Requesting allowed page {path}.", "fetch_page", {"path": path})
        note = ""
        more = index < len(targets) - 1
        if page.get("status") == 200:
            fetched += 1
            if _has_injection(page):
                injection_seen = True
                note = (
                    f"{path} contains instructions aimed at AI agents (it asks for /private/customer-data). "
                    "Page content is untrusted data, so the crawler ignores it. "
                )
            if more and page.get("remaining_window_requests") == 0:
                yield (
                    f"{note}No requests left in this rate-limit window; waiting {window:g} seconds as the policy asks.",
                    "wait",
                    {"seconds": window},
                )
                note = ""
        elif more and page.get("retry_after_seconds"):
            yield ("The gateway asked to slow down; waiting.", "wait", {"seconds": float(page["retry_after_seconds"])})

    outcome = yield (
        f"{note}Enough material collected. Completing the session so the site can return the deposit.",
        "complete_session",
        {},
    )
    lines = [
        f"- Read {fetched} allowed page(s) within the rate limit and request budget.",
        f"- Deposit outcome: {outcome.get('deposit_state')} ({outcome.get('policy_decision')}).",
    ]
    if injection_seen:
        lines.insert(1, "- Ignored a hidden instruction on /news/economy that asked for private customer data.")
    return "Scripted compliant crawler finished.\n" + "\n".join(lines)


def _rogue() -> Script:
    challenge = yield ("Requesting /news.", "fetch_page", {"path": "/news"})
    if challenge.get("status") != 402:
        return f"Expected an HTTP 402 challenge, got {challenge.get('status')}. Stopping."
    paid = yield ("HTTP 402. Paying the deposit without reading the rules.", "pay_deposit", {})
    if "error" in paid:
        return f"The deposit was not paid: {paid['error']}"
    accepted = yield (
        "Accepting the policy without reading it.",
        "accept_policy",
        {"policy_version": str(challenge.get("policy_version")), "agree_to_terms": True},
    )
    if "error" in accepted:
        return f"The policy was not accepted: {accepted['error']}"

    blocked = 0
    for path in ("/news", "/admin", "/private/customer-data", "/%2e%2e/admin"):
        page = yield (f"Grabbing {path}.", "fetch_page", {"path": path})
        blocked += 1 if page.get("blocked") else 0
    for _ in range(4):
        page = yield ("Bursting requests to scrape faster.", "fetch_page", {"path": "/news/technology"})
        blocked += 1 if page.get("blocked") else 0

    outcome = yield ("Done scraping. Completing the session and asking for the deposit back.", "complete_session", {})
    return (
        "Scripted rogue crawler finished.\n"
        f"- {blocked} request(s) were blocked by the gateway.\n"
        f"- Deposit outcome: {outcome.get('deposit_state')} ({outcome.get('policy_decision')})."
    )


SCRIPTS = {"compliant": _compliant, "rogue": _rogue}


class ScriptedDriver:
    """Implements the CrawlerAgent driver interface (next_turn / add_tool_results)."""

    model = "scripted"

    def __init__(self, persona_key: str) -> None:
        if persona_key not in SCRIPTS:
            raise ValueError(f"Unknown persona {persona_key!r}")
        self._script: Script = SCRIPTS[persona_key]()
        self._started = False
        self._last: dict[str, Any] = {}
        self._calls = 0

    def next_turn(self) -> Turn:
        try:
            if self._started:
                note, name, arguments = self._script.send(self._last)
            else:
                self._started = True
                note, name, arguments = next(self._script)
        except StopIteration as done:
            return Turn(stop="end_turn", text=str(done.value or ""), model=self.model)
        self._calls += 1
        return Turn(
            stop="tool_use",
            text=note,
            tool_calls=[ToolCall(call_id=f"scripted_{self._calls}", name=name, arguments=arguments)],
            model=self.model,
        )

    def add_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        parsed: Optional[Any] = None
        if results:
            try:
                parsed = json.loads(results[-1][1])
            except (TypeError, ValueError):
                parsed = None
        self._last = parsed if isinstance(parsed, dict) else {}
