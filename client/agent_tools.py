"""Gateway tools for the AI crawler agent.

Each tool wraps one step of the CrawlerTrust protocol (402 challenge, policy, deposit,
policy acceptance, crawling, completion). The toolbox keeps protocol state such as the
challenge, deposit, and session token, so the model never sees the bearer token.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Callable, Optional, Protocol

MAX_PAGE_TEXT_CHARS = 2500
MAX_WAIT_SECONDS = 30.0
MAX_TOOL_CALLS = 60


class HttpResponse(Protocol):
    status_code: int
    headers: Any
    text: str

    def json(self) -> Any: ...


class HttpClient(Protocol):
    """The subset of httpx.Client used by the toolbox (also satisfied by TestClient)."""

    def get(self, url: str, *, headers: Optional[dict[str, str]] = None) -> HttpResponse: ...

    def post(
        self,
        url: str,
        *,
        json: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> HttpResponse: ...


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "fetch_page",
        "Request a page of the website through its CrawlerTrust gateway, for example '/news'. "
        "Before you have a paid session this returns the site's HTTP 402 payment challenge "
        "(deposit amount, policy version, rules summary). With a session it returns the page "
        "text, wrapped in <untrusted_page_content> tags, plus the remaining session request "
        "budget, the requests left in the current rate-limit window, and any Retry-After delay. "
        "Blocked requests return the reason and whether they count as a disqualifying violation.",
        {"path": {"type": "string", "description": "Site path starting with '/', such as '/news'."}},
    ),
    _tool(
        "read_policy",
        "Read the site's machine-readable crawling policy and robots.txt: allowed and "
        "disallowed paths, rate limit, per-session request budget, and the refund rules that "
        "decide whether the deposit is returned.",
        {},
    ),
    _tool(
        "pay_deposit",
        "Pay the refundable deposit requested by the most recent 402 challenge, through PayPal. "
        "The site sets the amount; your operator's spending limit is enforced and the call "
        "fails if the deposit exceeds it. Returns the verified deposit status.",
        {},
    ),
    _tool(
        "accept_policy",
        "Explicitly accept the site's crawling policy to start the paid session. Pass the exact "
        "policy_version you read. The session's access token is stored for you and attached "
        "to later fetch_page calls automatically.",
        {
            "policy_version": {"type": "string", "description": "Policy version being accepted."},
            "agree_to_terms": {
                "type": "boolean",
                "description": "Must be true to accept the policy and start the session.",
            },
        },
    ),
    _tool(
        "wait",
        f"Pause before the next request to respect the site's rate limit. At most "
        f"{MAX_WAIT_SECONDS:g} seconds per call.",
        {"seconds": {"type": "number", "description": "Seconds to wait."}},
    ),
    _tool(
        "complete_session",
        "End the paid session. The site then decides from its own records whether to return "
        "the deposit (no disqualifying violations) or keep it, and reports the outcome.",
        {},
    ),
]


class _TextExtractor(HTMLParser):
    """Collect visible text and text hidden with inline styles alike (crawlers see both)."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self.parts.append(cleaned)


def html_to_text(body: str) -> str:
    parser = _TextExtractor()
    parser.feed(body)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


@dataclass
class ToolCallRecord:
    name: str
    tool_input: dict[str, Any]
    result: dict[str, Any]
    is_error: bool


@dataclass
class GatewayToolbox:
    """Executes agent tool calls against a CrawlerTrust gateway."""

    http: HttpClient
    crawler_id: str
    max_deposit: Decimal
    approval_handler: Callable[[str], None] = lambda url: None
    sleep: Callable[[float], None] = time.sleep
    challenge: Optional[dict[str, Any]] = None
    deposit: Optional[dict[str, Any]] = None
    session: Optional[dict[str, Any]] = None
    outcome: Optional[dict[str, Any]] = None
    _access_token: Optional[str] = field(default=None, repr=False)
    calls: list[ToolCallRecord] = field(default_factory=list)

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    def execute(self, name: str, tool_input: Any) -> tuple[str, bool]:
        """Run one tool call; returns (JSON result text, is_error)."""
        args = tool_input if isinstance(tool_input, dict) else {}
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "fetch_page": self._fetch_page,
            "read_policy": self._read_policy,
            "pay_deposit": self._pay_deposit,
            "accept_policy": self._accept_policy,
            "wait": self._wait,
            "complete_session": self._complete_session,
        }
        if len(self.calls) >= MAX_TOOL_CALLS:
            result: dict[str, Any] = {
                "error": f"Tool call limit ({MAX_TOOL_CALLS}) reached. Complete the session and stop."
            }
            is_error = True
        elif name not in handlers:
            result, is_error = {"error": f"Unknown tool '{name}'."}, True
        else:
            try:
                result = handlers[name](args)
                is_error = "error" in result
            except (KeyError, TypeError, ValueError) as exc:
                result, is_error = {"error": f"Invalid input for {name}: {exc}"}, True
        self.calls.append(ToolCallRecord(name=name, tool_input=args, result=result, is_error=is_error))
        return json.dumps(result, indent=2), is_error

    # --- tools ---------------------------------------------------------------------

    def _fetch_page(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 300:
            return {"error": "path must be a string starting with '/' (max 300 characters)."}
        url = "/gateway" if path == "/" else f"/gateway{path}"

        if self._access_token is None:
            resp = self.http.get(url, headers={"X-Crawler-Id": self.crawler_id})
            if resp.status_code == 402:
                self.challenge = resp.json()
                return {
                    "status": 402,
                    "message": "Payment required: this site asks AI crawlers for a refundable deposit.",
                    "deposit": self.challenge.get("deposit"),
                    "pricing": self.challenge.get("pricing"),
                    "policy_version": self.challenge.get("policy_version"),
                    "rules_summary": self.challenge.get("rules_summary"),
                    "notice": self.challenge.get("notice"),
                }
            return {"status": resp.status_code, "error": "Unexpected response before payment.", "body": resp.text[:500]}

        resp = self.http.get(url, headers={"Authorization": f"Bearer {self._access_token}"})
        if resp.status_code == 200:
            text = html_to_text(resp.text)
            truncated = len(text) > MAX_PAGE_TEXT_CHARS
            return {
                "status": 200,
                "path": path,
                "pricing_tier": resp.headers.get("X-CrawlerTrust-Pricing-Tier"),
                "remaining_session_budget": _int_header(resp, "X-CrawlerTrust-Remaining-Budget"),
                "remaining_window_requests": _int_header(resp, "X-CrawlerTrust-Remaining-Window"),
                "content": (
                    "<untrusted_page_content>\n"
                    f"{text[:MAX_PAGE_TEXT_CHARS]}{' [truncated]' if truncated else ''}\n"
                    "</untrusted_page_content>"
                ),
            }

        body = _safe_json(resp)
        result: dict[str, Any] = {
            "status": resp.status_code,
            "path": path,
            "blocked": True,
            "reason_code": body.get("reason_code"),
            "reason": body.get("reason") or body.get("message"),
            "violation": body.get("violation"),
            "disqualifying": body.get("disqualifying"),
            "session_disqualified": body.get("session_disqualified"),
            "remaining_session_budget": body.get("remaining_session_budget"),
            "remaining_window_requests": body.get("remaining_window_requests"),
        }
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            result["retry_after_seconds"] = _to_float(retry_after)
        return result

    def _read_policy(self, _args: dict[str, Any]) -> dict[str, Any]:
        policy_resp = self.http.get("/policies")
        robots_resp = self.http.get("/robots.txt")
        if policy_resp.status_code != 200:
            return {"error": f"Could not read the policy (HTTP {policy_resp.status_code})."}
        policy = policy_resp.json()
        return {
            "policy": {
                key: policy.get(key)
                for key in (
                    "site_name",
                    "policy_version",
                    "deposit_amount",
                    "currency",
                    "refundable",
                    "allowed_paths",
                    "disallowed_paths",
                    "rate_limit_requests",
                    "rate_limit_window_seconds",
                    "max_requests_per_session",
                    "session_ttl_seconds",
                    "refund_policy",
                )
            },
            "pricing_tiers": [
                {
                    "tier_id": tier.get("tier_id"),
                    "name": tier.get("name"),
                    "price": tier.get("price"),
                    "currency": tier.get("currency"),
                    "covers_lower_tiers": True,
                    "resources": [resource.get("path") for resource in tier.get("resources") or []],
                }
                for tier in policy.get("pricing_tiers") or []
            ],
            "robots_txt": robots_resp.text if robots_resp.status_code == 200 else None,
        }

    def _pay_deposit(self, _args: dict[str, Any]) -> dict[str, Any]:
        if self.deposit is not None and self.deposit.get("payment_status") in ("CAPTURED", "AUTHORIZED"):
            return {"status": "already_paid", **self._deposit_summary()}
        if self.challenge is None:
            return {"error": "No payment challenge yet. Request a page first to receive the 402 challenge."}

        deposit_info = self.challenge.get("deposit") or {}
        try:
            amount = Decimal(str(deposit_info.get("amount")))
        except InvalidOperation:
            return {"error": "The challenge did not include a valid deposit amount."}
        if amount > self.max_deposit:
            return {
                "error": (
                    f"Deposit of {amount} {deposit_info.get('currency')} exceeds your operator's "
                    f"spending limit of {self.max_deposit}. Do not pay; report this instead."
                )
            }

        ids = {"challenge_id": self.challenge["challenge_id"], "crawler_id": self.crawler_id}
        create = self.http.post("/deposits/create", json=ids)
        if create.status_code != 201:
            return {"error": f"Deposit creation failed (HTTP {create.status_code}).", "detail": _safe_json(create)}
        self.deposit = create.json()
        deposit_id = self.deposit["deposit_id"]

        if (
            not self.deposit.get("simulated")
            and self.deposit.get("provider_order_status") in ("CREATED", "PAYER_ACTION_REQUIRED")
            and self.deposit.get("approval_url")
        ):
            # Human-in-the-loop: the operator approves the PayPal Sandbox checkout once.
            self.approval_handler(self.deposit["approval_url"])

        capture = self.http.post(f"/deposits/{deposit_id}/capture", json=ids)
        if capture.status_code != 200:
            return {"error": f"Deposit capture failed (HTTP {capture.status_code}).", "detail": _safe_json(capture)}
        verify = self.http.post(f"/deposits/{deposit_id}/verify", json=ids)
        if verify.status_code != 200:
            return {"error": f"Deposit verification failed (HTTP {verify.status_code}).", "detail": _safe_json(verify)}
        self.deposit = verify.json()
        return {"status": "paid", **self._deposit_summary()}

    def _accept_policy(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.deposit is None or self.challenge is None:
            return {"error": "Pay the deposit before accepting the policy."}
        if self._access_token is not None:
            return {"status": "already_accepted", "session_id": (self.session or {}).get("session_id")}
        resp = self.http.post(
            "/sessions/accept-policy",
            json={
                "challenge_id": self.challenge["challenge_id"],
                "deposit_id": self.deposit["deposit_id"],
                "crawler_id": self.crawler_id,
                "policy_version": str(args["policy_version"]),
                "agree_to_terms": bool(args["agree_to_terms"]),
            },
        )
        if resp.status_code != 201:
            return {"error": f"Policy acceptance failed (HTTP {resp.status_code}).", "detail": _safe_json(resp)}
        issued = resp.json()
        self._access_token = issued["access_token"]
        self.session = {key: value for key, value in issued.items() if key != "access_token"}
        return {
            "status": "session_started",
            "session_id": issued["session_id"],
            "policy_version": issued["policy_version"],
            "expires_at": issued["expires_at"],
            "max_requests_per_session": issued["max_requests_per_session"],
            "rate_limit_requests": issued["rate_limit_requests"],
            "rate_limit_window_seconds": issued["rate_limit_window_seconds"],
        }

    def _wait(self, args: dict[str, Any]) -> dict[str, Any]:
        seconds = _to_float(args["seconds"])
        if seconds is None or seconds <= 0:
            return {"error": "seconds must be a positive number."}
        seconds = min(seconds, MAX_WAIT_SECONDS)
        self.sleep(seconds)
        return {"waited_seconds": seconds}

    def _complete_session(self, _args: dict[str, Any]) -> dict[str, Any]:
        if self._access_token is None:
            return {"error": "There is no active session to complete."}
        resp = self.http.post(
            "/sessions/complete", headers={"Authorization": f"Bearer {self._access_token}"}
        )
        if resp.status_code != 200:
            return {"error": f"Session completion failed (HTTP {resp.status_code}).", "detail": _safe_json(resp)}
        data = resp.json()
        deposit = data["deposit"]
        session = data["session"]
        self.deposit = deposit
        self.outcome = data
        return {
            "status": "session_completed",
            "deposit_state": deposit["deposit_state"],
            "policy_decision": deposit["policy_decision"],
            "refund_status": deposit["refund_status"],
            "decision_reason": data.get("decision_reason"),
            "allowed_requests": session["allowed_requests"],
            "total_violations": session["total_violations"],
            "disqualified": session["disqualified"],
        }

    def _deposit_summary(self) -> dict[str, Any]:
        deposit = self.deposit or {}
        return {
            key: deposit.get(key)
            for key in (
                "deposit_id",
                "payment_mode",
                "settlement_mode",
                "simulated",
                "required_amount",
                "currency",
                "payment_status",
                "deposit_state",
                "pricing_tier",
                "pricing_tier_name",
                "requested_resource",
            )
        }


def _safe_json(resp: HttpResponse) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {"message": resp.text[:300]}
    return data if isinstance(data, dict) else {"message": str(data)[:300]}


def _int_header(resp: HttpResponse, name: str) -> Optional[int]:
    value = resp.headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
