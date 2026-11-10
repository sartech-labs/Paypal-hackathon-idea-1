"""Guarded agent prompt and tool for the crawler playground.

The model may only invoke the curated actions the operator selected. Each ``run_action``
call executes that action end-to-end against the live gateway with its own fresh challenge,
deposit, and session. The model plans the order, reads the gateway's responses, and writes
the final brief; it never receives a session bearer token.
"""

from __future__ import annotations

from typing import Any

from server.services.ai_client import ToolSpec

PLAYGROUND_AGENT_SYSTEM = """\
You are the operator of a crawler in SARA's authorized red-team playground. This is a
demo site with synthetic content and a real gateway. Your job is to run the actions the
operator selected, one at a time, and explain exactly what the gateway did.

Rules:
1. Call run_action exactly once for every selected action. Never skip one.
2. Only use action IDs from the selected checklist. Never invent an action or a path.
3. Run allowed/read actions before rule-breaking actions so the reader can see the normal
   flow first.
4. After each call, read the structured result. It contains the HTTP status, matched rule,
   agent-facing reason, and deposit consequence returned by the gateway.
5. Do not claim an action succeeded unless its result says the gateway allowed it.
6. When every selected action has run, write a short report: what was allowed, what was
   blocked, which rule matched, and what happened to each deposit. Do not include secrets
   or guess at content you did not receive.

The selected actions and the live policy are supplied in the task. A session token is held
by the playground and never shown to you.
"""

PLAYGROUND_ACTION_TOOL = ToolSpec(
    name="run_action",
    description=(
        "Run one selected playground action against the live SARA gateway. Use the exact "
        "action_id from the selected checklist. Each action gets a fresh payment challenge, "
        "deposit, and session where the action needs one. Returns the gateway's response and "
        "the final deposit consequence."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "description": "One action_id from the selected checklist.",
            }
        },
        "required": ["action_id"],
        "additionalProperties": False,
    },
    strict=True,
)


def build_agent_prompt(actions: list[dict[str, Any]], policy: dict[str, Any]) -> str:
    """Describe the selected checklist and live limits as untrusted-but-factual data."""
    lines = [
        "Selected actions (run each exactly once):",
    ]
    for index, action in enumerate(actions, start=1):
        lines.append(
            f"{index}. {action['action_id']} — {action['label']} ({action['tone']}): "
            f"{action['description']} Expected: {action['expected']}"
        )
    lines.extend(
        [
            "",
            "Live policy limits:",
            f"- policy_version: {policy.get('policy_version')}",
            f"- allowed_paths: {', '.join(policy.get('allowed_paths') or []) or '(none)'}",
            f"- disallowed_paths: {', '.join(policy.get('disallowed_paths') or []) or '(none)'}",
            f"- rate_limit: {policy.get('rate_limit_requests')} requests per "
            f"{policy.get('rate_limit_window_seconds')} seconds",
            f"- max_requests_per_session: {policy.get('max_requests_per_session')}",
            f"- refundable_deposit: {policy.get('deposit_amount')} {policy.get('currency')}",
            "",
            "Call run_action for the first selected action now.",
        ]
    )
    return "\n".join(lines)
