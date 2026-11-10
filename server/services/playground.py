"""Curated crawler playground: deterministic good/bad actions against the live gateway.

Each action runs a fixed sequence of HTTP calls against this same gateway, using the real
deposit, policy, rate-limit, and settlement machinery. Actions auto-complete their session
so the deposit consequence shown in the console is final. No AI model is involved.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.core.agent_identity import sign_request
from server.core.quotas import HourlyQuota
from server.core.security import utc_now_iso
from server.schemas import (
    PlaygroundActionInfo,
    PlaygroundRecentRun,
    PlaygroundRunResponse,
    PlaygroundStep,
    PlaygroundViolation,
)

logger = logging.getLogger("sara.playground")

MAX_RUNS_KEPT = 20
MAX_HTML_BYTES = 400_000
# A playground run holds the single run slot, so every pause is capped and every run has a
# hard deadline: a long rate-limit window must never make a demo wait for minutes.
MAX_INTER_REQUEST_PAUSE_SECONDS = 2.0
DEFAULT_RUN_TIMEOUT_SECONDS = 60.0
MAX_BURST_REQUESTS = 50
# Last-resort page for a policy that advertises no usable page paths.
DEFAULT_PAGE_PATH = "/news"
# Agent-facing response headers the gateway sets on allowed gateway responses.
AGENT_RESPONSE_HEADERS = (
    "X-SARA-Session-Id",
    "X-SARA-Policy-Version",
    "X-SARA-Remaining-Budget",
    "X-SARA-Remaining-Window",
    "X-SARA-Pricing-Tier",
)

ACTIONS: tuple[dict[str, str], ...] = (
    {
        "action_id": "allowed_page",
        "label": "Allowed page",
        "tone": "good",
        "description": "Open a session with the required deposit, send a valid paid token, and read an allowed page.",
        "expected": "HTTP 200 with the rendered page; deposit returned.",
    },
    {
        "action_id": "deposit_and_crawl",
        "label": "Deposit + crawl within budget",
        "tone": "good",
        "description": "Deposit, then read three allowed pages without exceeding the session request budget.",
        "expected": "Three HTTP 200s; deposit returned.",
    },
    {
        "action_id": "polite_pace",
        "label": "Polite pace",
        "tone": "good",
        "description": "Read allowed pages while waiting out the rate-limit window instead of bursting.",
        "expected": "No 429s; deposit returned.",
    },
    {
        "action_id": "valid_paid_token",
        "label": "Valid paid token",
        "tone": "good",
        "description": "Show that a paid session bearer token opens an allowed resource and carries SARA response headers.",
        "expected": "HTTP 200 with X-SARA-Session-Id and remaining budget headers.",
    },
    {
        "action_id": "disallowed_path",
        "label": "Disallowed path",
        "tone": "bad",
        "description": "Pay the deposit, then request a path the site's rules forbid.",
        "expected": "HTTP 403 with the matched Disallow rule; deposit forfeited.",
    },
    {
        "action_id": "rate_limit_burst",
        "label": "Rate-limit burst",
        "tone": "bad",
        "description": "Pay the deposit, then fire requests faster than the published rate limit.",
        "expected": "HTTP 429 RATE_LIMIT_EXCEEDED; deposit forfeited after the violation threshold.",
    },
    {
        "action_id": "budget_overrun",
        "label": "Budget overrun",
        "tone": "bad",
        "description": "Pay the deposit, then exceed the per-session request budget.",
        "expected": "HTTP 429 MAX_REQUESTS_EXCEEDED; deposit forfeited.",
    },
    {
        "action_id": "no_deposit",
        "label": "No deposit",
        "tone": "bad",
        "description": "Ask for protected content with an agent identity but without paying the deposit.",
        "expected": "HTTP 402 with the deposit challenge; no deposit taken.",
    },
    {
        "action_id": "path_traversal",
        "label": "Path traversal",
        "tone": "bad",
        "description": "Pay the deposit, then try an encoded traversal path aimed at the admin area.",
        "expected": "HTTP 403 INVALID_PATH; deposit forfeited.",
    },
    {
        "action_id": "forged_signature",
        "label": "Forged signature",
        "tone": "bad",
        "description": "Send a Web Bot Auth signature from a key the site does not know.",
        "expected": "HTTP 401 INVALID_SIGNATURE; no deposit taken.",
    },
)

ACTIONS_BY_ID = {action["action_id"]: action for action in ACTIONS}


def action_infos() -> list[PlaygroundActionInfo]:
    return [PlaygroundActionInfo(**action) for action in ACTIONS]


@dataclass
class PlaygroundRun:
    run_id: str
    action_id: str
    label: str
    tone: str
    status: str = "running"
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: Optional[str] = None
    outcome: Optional[str] = None
    rendered_html: Optional[str] = None
    content_type: Optional[str] = None
    violation: Optional[PlaygroundViolation] = None
    deposit: Optional[dict[str, Any]] = None
    session_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    steps: list[PlaygroundStep] = field(default_factory=list)
    audit_event_ids: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_monotonic: float = field(default_factory=time.monotonic)
    deadline_monotonic: Optional[float] = None

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def emit(
        self,
        label: str,
        *,
        request: Optional[str] = None,
        http_status: Optional[int] = None,
        reason_code: Optional[str] = None,
        allowed: Optional[bool] = None,
        message: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        with self.lock:
            self.steps.append(
                PlaygroundStep(
                    seq=len(self.steps),
                    at=utc_now_iso(),
                    label=label,
                    request=request,
                    http_status=http_status,
                    reason_code=reason_code,
                    allowed=allowed,
                    message=message,
                    detail=detail or {},
                )
            )

    def update(self, **changes: Any) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self, key, value)

    def snapshot(self, after: int = 0) -> PlaygroundRunResponse:
        with self.lock:
            return PlaygroundRunResponse(
                run_id=self.run_id,
                action_id=self.action_id,
                label=self.label,
                tone=self.tone,  # type: ignore[arg-type]
                status=self.status,  # type: ignore[arg-type]
                started_at=self.started_at,
                finished_at=self.finished_at,
                outcome=self.outcome,  # type: ignore[arg-type]
                rendered_html=self.rendered_html,
                content_type=self.content_type,
                violation=self.violation,
                deposit=self.deposit,
                session_id=self.session_id,
                steps=[step for step in self.steps if step.seq >= after],
                next_seq=len(self.steps),
                error=self.error,
                audit_event_ids=list(self.audit_event_ids),
                elapsed_ms=self.elapsed_ms,
            )

    def summary(self) -> PlaygroundRecentRun:
        deposit = self.deposit or {}
        violation = self.violation
        return PlaygroundRecentRun(
            run_id=self.run_id,
            action_id=self.action_id,
            label=self.label,
            tone=self.tone,  # type: ignore[arg-type]
            status=self.status,
            outcome=self.outcome,
            started_at=self.started_at,
            deposit_state=deposit.get("deposit_state"),
            reason_code=violation.reason_code if violation else None,
        )


class PlaygroundBusyError(RuntimeError):
    status_code = 409
    code = "playground_run_in_progress"


class PlaygroundQuotaError(RuntimeError):
    status_code = 429
    code = "playground_quota_exceeded"


class PlaygroundActionError(RuntimeError):
    status_code = 400
    code = "playground_action_error"


class PlaygroundTimeoutError(RuntimeError):
    """Raised when a run exceeds its hard deadline and must stop."""


def _default_http_factory(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=60.0)


class PlaygroundRunner:
    """Owns playground runs: one active at a time, capped per hour."""

    def __init__(
        self,
        *,
        max_runs_per_hour: int,
        http_factory: Optional[Callable[[str], Any]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
    ) -> None:
        self._quota = HourlyQuota(max_runs_per_hour)
        self._http_factory = http_factory or _default_http_factory
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._run_timeout_seconds = max(1.0, float(run_timeout_seconds))
        self._runs: dict[str, PlaygroundRun] = {}
        self._order: list[str] = []
        self._active: Optional[str] = None
        self._lock = threading.Lock()

    def runs_remaining(self) -> int:
        return self._quota.remaining()

    def active_run_id(self) -> Optional[str]:
        with self._lock:
            run = self._runs.get(self._active) if self._active else None
            return run.run_id if run is not None and not run.finished else None

    def get(self, run_id: str) -> Optional[PlaygroundRun]:
        with self._lock:
            return self._runs.get(run_id)

    def recent(self, limit: int = 8) -> list[PlaygroundRecentRun]:
        with self._lock:
            runs = [self._runs[run_id] for run_id in reversed(self._order[-limit:])]
        return [run.summary() for run in runs]

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.finished:
            return False
        run.cancel_event.set()
        return True

    def start(
        self,
        *,
        action_id: str,
        base_url: str,
        policy: dict[str, Any],
        audit: Optional[Callable[[str, str, dict[str, Any]], Optional[str]]] = None,
        policy_version: Optional[str] = None,
        payment_mode: Optional[str] = None,
    ) -> PlaygroundRun:
        action = ACTIONS_BY_ID.get(action_id)
        if action is None:
            raise PlaygroundActionError(f"Unknown playground action: {action_id}")
        with self._lock:
            active = self._runs.get(self._active) if self._active else None
            if active is not None and not active.finished:
                raise PlaygroundBusyError("A playground run is already in progress.")
            if not self._quota.try_acquire():
                raise PlaygroundQuotaError("The hourly playground limit was reached. Try again later.")
            run = PlaygroundRun(
                run_id=f"pg_{secrets.token_hex(6)}",
                action_id=action_id,
                label=action["label"],
                tone=action["tone"],
            )
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            while len(self._order) > MAX_RUNS_KEPT:
                self._runs.pop(self._order.pop(0), None)
            self._active = run.run_id

        thread = threading.Thread(
            target=self._execute,
            args=(run, base_url, policy, audit, policy_version, payment_mode),
            name=f"playground-{run.run_id}",
            daemon=True,
        )
        thread.start()
        return run

    # --- execution ------------------------------------------------------------------

    def _execute(
        self,
        run: PlaygroundRun,
        base_url: str,
        policy: dict[str, Any],
        audit: Optional[Callable[[str, str, dict[str, Any]], Optional[str]]],
        policy_version: Optional[str],
        payment_mode: Optional[str],
    ) -> None:
        def log(event_type: str, reason_code: str, details: dict[str, Any]) -> None:
            if audit is None:
                return
            try:
                event_id = audit(event_type, reason_code, details)
                if event_id:
                    run.audit_event_ids.append(event_id)
            except Exception:  # audit must never break a run
                logger.exception("Playground audit write failed for %s", run.run_id)

        log(
            "PLAYGROUND_RUN_STARTED",
            "PLAYGROUND_RUN",
            {"run_id": run.run_id, "action_id": run.action_id, "label": run.label, "tone": run.tone},
        )
        headers = {
            # A non-browser User-Agent is what marks playground traffic as an agent.
            "User-Agent": "SARA-Playground/1.0",
            "X-SARA-Playground-Run": run.run_id,
        }
        result: dict[str, Any] = {}
        run.update(deadline_monotonic=self._clock() + self._run_timeout_seconds)
        try:
            with self._http_factory(base_url) as raw_http:
                http = _DeadlineHttp(raw_http, lambda: self._check_deadline(run))
                result = self._dispatch(run, http, headers, policy, policy_version, payment_mode)
        except PlaygroundTimeoutError as exc:
            logger.warning("Playground run %s hit its time limit", run.run_id)
            run.emit(
                "Run stopped by the playground time limit",
                message=str(exc),
                detail={"time_limit_seconds": self._run_timeout_seconds},
            )
            run.update(status="failed", outcome="error", error=str(exc), finished_at=utc_now_iso())
        except Exception as exc:
            logger.exception("Playground run %s failed", run.run_id)
            run.update(status="failed", outcome="error", error=f"{type(exc).__name__}: {exc}", finished_at=utc_now_iso())
            run.emit("Run failed", message=run.error, detail={"error": type(exc).__name__})
        else:
            run.update(status="completed", finished_at=utc_now_iso(), **result)
        finally:
            run.update(elapsed_ms=int((time.monotonic() - run.started_monotonic) * 1000))
            log(
                "PLAYGROUND_RUN_FINISHED",
                _terminal_reason_code(run),
                {
                    "run_id": run.run_id,
                    "action_id": run.action_id,
                    "outcome": run.outcome,
                    "status": run.status,
                    "deposit_state": (run.deposit or {}).get("deposit_state"),
                    "reason_code": run.violation.reason_code if run.violation else None,
                },
            )

    def _dispatch(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        policy_version: Optional[str],
        payment_mode: Optional[str],
    ) -> dict[str, Any]:
        action = run.action_id
        plan = _page_plan(policy)
        if plan.fallback:
            run.emit(
                "No usable page paths in the current policy",
                message=(
                    "The current site policy advertises no usable page paths, so the playground "
                    f"falls back to {plan.challenge}."
                ),
                detail={"path": plan.challenge},
            )
        if action == "no_deposit":
            return self._no_deposit(run, http, headers, policy, plan)
        if action == "forged_signature":
            return self._forged_signature(run, http, headers, policy, plan)
        session = self._paid_session(run, http, headers, policy, policy_version, payment_mode, plan)
        if action == "allowed_page":
            return self._allowed_page(run, http, headers, session, policy, plan, plan.challenge)
        if action == "valid_paid_token":
            path = plan.reads[0] if plan.reads else plan.challenge
            return self._allowed_page(run, http, headers, session, policy, plan, path)
        if action == "deposit_and_crawl":
            return self._deposit_and_crawl(run, http, headers, session, policy, plan)
        if action == "polite_pace":
            return self._polite_pace(run, http, headers, session, policy, plan)
        if action == "disallowed_path":
            return self._disallowed_path(run, http, headers, session, policy)
        if action == "rate_limit_burst":
            return self._rate_limit_burst(run, http, headers, session, policy, plan)
        if action == "budget_overrun":
            return self._budget_overrun(run, http, headers, session, policy, plan)
        if action == "path_traversal":
            return self._path_traversal(run, http, headers, session, policy)
        raise PlaygroundActionError(f"Unknown playground action: {action}")

    # --- actions --------------------------------------------------------------------

    def _paid_session(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        policy_version: Optional[str],
        payment_mode: Optional[str],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        crawler_id = f"playground-{run.run_id[-6:]}"
        challenge_headers = {**headers, "X-Crawler-Id": crawler_id}
        challenge_path = plan.challenge
        challenge_resp = http.get(challenge_path, headers=challenge_headers)
        run.emit(
            "Request a protected page without a token",
            request=f"GET {challenge_path}",
            http_status=challenge_resp.status_code,
            reason_code=_reason(challenge_resp),
            allowed=None,
            message="HTTP 402 challenges the agent to post the site-priced deposit.",
        )
        if challenge_resp.status_code != 402:
            raise PlaygroundActionError(
                f"Expected a 402 challenge, got HTTP {challenge_resp.status_code}."
            )
        challenge = challenge_resp.json()
        crawler_id = challenge.get("crawler_id") or crawler_id
        challenge_id = challenge["challenge_id"]
        deposit = http.post(
            "/deposits/create",
            json={"challenge_id": challenge_id, "crawler_id": crawler_id},
        )
        if deposit.status_code != 201:
            raise PlaygroundActionError(
                f"Deposit creation failed with HTTP {deposit.status_code}: {_message(deposit)}"
            )
        deposit_body = deposit.json()
        deposit_id = deposit_body["deposit_id"]
        ids = {"crawler_id": crawler_id, "challenge_id": challenge_id}
        capture = http.post(f"/deposits/{deposit_id}/capture", json=ids)
        run.emit(
            "Pay the refundable deposit",
            request=f"POST /deposits/{deposit_id}/capture",
            http_status=capture.status_code,
            reason_code=_reason(capture),
            allowed=capture.status_code == 200,
            message=_message(capture) or f"Deposit {deposit_body.get('required_amount')} {deposit_body.get('currency')}",
            detail={"deposit_id": deposit_id, "amount": deposit_body.get("required_amount"), "mode": payment_mode},
        )
        if capture.status_code != 200:
            raise PlaygroundActionError(
                f"Deposit payment did not complete (HTTP {capture.status_code}). "
                "In PayPal Sandbox mode, enable PAYPAL_SANDBOX_AUTOPAY or use mock payments."
            )
        verify = http.post(f"/deposits/{deposit_id}/verify", json=ids)
        if verify.status_code != 200:
            raise PlaygroundActionError(
                f"Deposit verification failed with HTTP {verify.status_code}: {_message(verify)}"
            )
        accepted = http.post(
            "/sessions/accept-policy",
            json={
                **ids,
                "deposit_id": deposit_id,
                "policy_version": policy_version or challenge.get("policy_version"),
                "agree_to_terms": True,
            },
        )
        if accepted.status_code != 201:
            raise PlaygroundActionError(
                f"Policy acceptance failed with HTTP {accepted.status_code}: {_message(accepted)}"
            )
        session = accepted.json()
        run.update(session_id=session.get("session_id"))
        run.emit(
            "Accept the policy and open a paid session",
            request="POST /sessions/accept-policy",
            http_status=accepted.status_code,
            allowed=True,
            message=f"Session {session.get('session_id')} issued; token is held by the crawler.",
            detail={"session_id": session.get("session_id"), "deposit_id": deposit_id},
        )
        return {
            "crawler_id": crawler_id,
            "challenge_id": challenge_id,
            "deposit_id": deposit_id,
            "deposit": deposit_body,
            "session": session,
            "token": session.get("access_token"),
        }

    def _record_response(
        self,
        run: PlaygroundRun,
        label: str,
        request: str,
        resp: Any,
        *,
        include_body: bool = False,
        note: Optional[str] = None,
    ) -> None:
        detail: dict[str, Any] = {}
        agent_headers = _agent_response_headers(resp)
        if agent_headers:
            detail["response_headers"] = agent_headers
        if include_body:
            content_type = resp.headers.get("content-type", "")
            detail["content_type"] = content_type
            detail["body_preview"] = resp.text[:500]
        message = note or _message(resp)
        if message is None and agent_headers:
            message = _header_summary(agent_headers)
        run.emit(
            label,
            request=request,
            http_status=resp.status_code,
            reason_code=_reason(resp),
            allowed=resp.status_code == 200,
            message=message,
            detail=detail,
        )

    def _read_pages(
        self,
        run: PlaygroundRun,
        http: Any,
        token_headers: dict[str, str],
        policy: dict[str, Any],
        plan: _PagePlan,
        paths: list[str],
        *,
        label: str,
        pace: bool,
    ) -> dict[str, Any]:
        """Read `paths` with the paid token, tolerating an origin with no page there.

        A 404 that still carries SARA session headers means the gateway *authorized* the
        request and the origin had nothing to serve: that is a content gap, never a policy
        refusal, so the reader swaps in another allowed page (bounded) and never reports it
        as a violation.
        """
        queue = list(paths)
        attempted = set(queue)
        responses: list[Any] = []
        misses: list[str] = []
        refusal: Optional[Any] = None
        refusal_path: Optional[str] = None
        index = 0
        limit = len(paths) + len(plan.pages)
        while queue and index < limit:
            if run.cancel_event.is_set():
                break
            path = queue.pop(0)
            index += 1
            if index > 1 and pace:
                self._pause(run, _pace_seconds(policy))
            resp = http.get(path, headers=token_headers)
            miss = _origin_miss(resp)
            total = max(len(paths), index + len(queue))
            self._record_response(
                run,
                f"{label} {index}/{total}",
                f"GET {path}",
                resp,
                include_body=resp.status_code == 200,
                note=(
                    f"The gateway authorized GET {path}, but the site origin returned HTTP 404 "
                    "(no page is published at that allowed path)."
                )
                if miss
                else None,
            )
            if resp.status_code == 200:
                responses.append(resp)
                continue
            if miss:
                misses.append(path)
                replacement = next((page for page in plan.pages if page not in attempted), None)
                if replacement is not None:
                    attempted.add(replacement)
                    queue.append(replacement)
                continue
            refusal = resp
            refusal_path = path
            break
        return {
            "responses": responses,
            "misses": misses,
            "refusal": refusal,
            "refusal_path": refusal_path,
        }

    def _readable_response(self, run: PlaygroundRun, result: dict[str, Any]) -> Optional[Any]:
        """Return the last readable page, explaining an origin with nothing to serve."""
        responses = result["responses"]
        if responses:
            return responses[-1]
        if result["misses"]:
            run.emit(
                "No page published at the allowed paths",
                message=(
                    "The gateway allowed every request, but the configured site published no page "
                    "at: "
                    + ", ".join(result["misses"])
                    + ". The policy is working; the site has no content at those paths."
                ),
                detail={"missing_paths": result["misses"]},
            )
        return None

    def _allowed_page(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
        plan: _PagePlan,
        path: str,
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        result = self._read_pages(
            run,
            http,
            token_headers,
            policy,
            plan,
            [path],
            label="Read the allowed page with the paid token",
            pace=False,
        )
        if result["refusal"] is not None:
            refusal = result["refusal"]
            return self._refused(
                run, http, headers, session, result["refusal_path"] or path, refusal, refusal.status_code, policy=policy
            )
        readable = self._readable_response(run, result)
        deposit = self._complete_and_settle(run, http, headers, session)
        return {
            "outcome": "allowed",
            "rendered_html": readable.text[:MAX_HTML_BYTES] if readable is not None else None,
            "content_type": readable.headers.get("content-type") if readable is not None else None,
            "deposit": deposit,
        }

    def _deposit_and_crawl(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        budget = max(1, int(policy.get("max_requests_per_session") or 3))
        count = min(3, budget)
        paths = plan.repeat(count)
        if count < 3:
            run.emit(
                "Session budget allows fewer pages",
                message=(
                    f"The current policy allows {budget} request(s) per session, so the crawl reads "
                    f"{count} page(s) instead of three and stays inside the budget."
                ),
                detail={"max_requests_per_session": budget, "pages": count},
            )
        if len(plan.pages) < count:
            run.emit(
                "Limited set of allowed pages",
                message=(
                    f"The current policy exposes {len(plan.pages)} usable page path(s), so the crawl "
                    "repeats them instead of requesting pages the site does not allow."
                ),
                detail={"pages": list(plan.pages)},
            )
        result = self._read_pages(
            run, http, token_headers, policy, plan, paths, label="Read allowed page", pace=True
        )
        if result["refusal"] is not None:
            refusal = result["refusal"]
            return self._refused(
                run, http, headers, session, result["refusal_path"] or plan.challenge, refusal, refusal.status_code, policy=policy
            )
        readable = self._readable_response(run, result)
        deposit = self._complete_and_settle(run, http, headers, session)
        return {
            "outcome": "allowed",
            "rendered_html": readable.text[:MAX_HTML_BYTES] if readable is not None else None,
            "content_type": readable.headers.get("content-type") if readable is not None else None,
            "deposit": deposit,
        }

    def _polite_pace(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        budget = int(policy.get("max_requests_per_session") or 3)
        window_allowance = max(1, int(policy.get("rate_limit_requests") or 3))
        # Never ask for more requests in one run than the published window allows: with a long
        # window the cool-down pause is capped, so extra requests would trip the limiter.
        count = max(1, min(3, budget, window_allowance))
        result = self._read_pages(
            run,
            http,
            token_headers,
            policy,
            plan,
            [plan.challenge] * count,
            label="Paced request",
            pace=True,
        )
        if result["refusal"] is not None:
            refusal = result["refusal"]
            return self._refused(run, http, headers, session, plan.challenge, refusal, refusal.status_code, policy=policy)
        readable = self._readable_response(run, result)
        deposit = self._complete_and_settle(run, http, headers, session)
        return {
            "outcome": "allowed",
            "rendered_html": readable.text[:MAX_HTML_BYTES] if readable is not None else None,
            "content_type": readable.headers.get("content-type") if readable is not None else None,
            "deposit": deposit,
        }

    def _disallowed_path(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        path = _first_disallowed(policy)
        if path is None:
            raise PlaygroundActionError(
                "The current site policy lists no disallowed paths, so this action cannot be "
                "demonstrated. Add a disallowed path (for example /admin) and try again."
            )
        resp = http.get(path, headers=token_headers)
        self._record_response(run, "Request a disallowed path", f"GET {path}", resp)
        return self._refused(run, http, headers, session, path, resp, resp.status_code, policy=policy)

    def _rate_limit_burst(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        limit = max(1, int(policy.get("rate_limit_requests") or 3))
        violations_target = max(
            2, int(policy.get("refund_policy", {}).get("max_rate_limit_violations_before_disqualification") or 2)
        )
        count = min(limit + violations_target, MAX_BURST_REQUESTS)
        path = plan.challenge
        denied: Optional[tuple[Any, str]] = None
        last: Optional[Any] = None
        # No pauses here: a burst must stay back-to-back so it still trips the real limiter.
        for index in range(count):
            if run.cancel_event.is_set():
                break
            resp = http.get(path, headers=token_headers)
            last = resp
            self._record_response(run, f"Burst request {index + 1}/{count}", f"GET {path}", resp)
            if resp.status_code == 429:
                # Keep the terminal denial: it is the one that disqualifies the session.
                denied = (resp, path)
        if denied is None:
            deposit = self._complete_and_settle(run, http, headers, session)
            run.emit(
                "Rate limit not reached",
                message=(
                    f"{count} back-to-back requests stayed inside the published limit of "
                    f"{limit} request(s) per {float(policy.get('rate_limit_window_seconds') or 0):g}s."
                ),
                detail={"requests": count, "limit": limit},
            )
            return {
                "outcome": "allowed",
                "rendered_html": last.text[:MAX_HTML_BYTES] if last is not None and last.status_code == 200 else None,
                "content_type": last.headers.get("content-type") if last is not None and last.status_code == 200 else None,
                "deposit": deposit,
            }
        resp, denied_path = denied
        return self._refused(run, http, headers, session, denied_path, resp, resp.status_code, policy=policy)

    def _budget_overrun(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        budget = max(1, int(policy.get("max_requests_per_session") or 10))
        path = plan.challenge
        allowed = 0
        last = None
        while allowed <= budget and not run.cancel_event.is_set():
            resp = http.get(path, headers=token_headers)
            last = resp
            reason = _reason(resp)
            self._record_response(run, f"Budget request {allowed + 1}", f"GET {path}", resp)
            if resp.status_code == 200 or _origin_miss(resp):
                # An origin 404 still consumed budget: the gateway authorized the request.
                allowed += 1
                continue
            if resp.status_code == 429 and reason == "RATE_LIMIT_EXCEEDED":
                self._pause(run, min(_retry_after(resp), MAX_INTER_REQUEST_PAUSE_SECONDS))
                continue
            if reason == "MAX_REQUESTS_EXCEEDED":
                break
            break
        if last is None:
            raise PlaygroundActionError("No response received while testing the budget.")
        return self._refused(run, http, headers, session, path, last, last.status_code, policy=policy)

    def _path_traversal(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        path = "/%2e%2e/admin"
        resp = http.get(path, headers=token_headers)
        self._record_response(run, "Attempt an encoded path traversal", f"GET {path}", resp)
        return self._refused(run, http, headers, session, path, resp, resp.status_code, policy=policy)

    def _no_deposit(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        path = plan.challenge
        resp = http.get(path, headers=headers)
        self._record_response(run, "Request content without paying a deposit", f"GET {path}", resp)
        violation = _violation_from_response(resp, policy, action="no_deposit", deposit=None)
        return {
            "outcome": "refused",
            "violation": violation,
            "deposit": {"deposit_state": None, "consequence": "none (no deposit was taken)"},
        }

    def _forged_signature(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        plan: _PagePlan,
    ) -> dict[str, Any]:
        key = Ed25519PrivateKey.generate()
        path = plan.challenge
        authority = urlsplit(str(getattr(http, "base_url", "http://127.0.0.1:8000"))).netloc or "127.0.0.1:8000"
        signed = sign_request(key, "GET", authority, path)
        resp = http.get(path, headers={**headers, **signed})
        self._record_response(run, "Send a forged Web Bot Auth signature", f"GET {path}", resp)
        violation = _violation_from_response(resp, policy, action="forged_signature", deposit=None)
        return {
            "outcome": "refused",
            "violation": violation,
            "deposit": {"deposit_state": None, "consequence": "none (no deposit was taken)"},
        }

    # --- settlement and helpers -----------------------------------------------------

    def _refused(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
        path: str,
        resp: Any,
        http_status: int,
        *,
        policy: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        deposit = self._complete_and_settle(run, http, headers, session)
        violation = _violation_from_response(resp, policy or {}, action=run.action_id, deposit=deposit)
        # The settled deposit is the authoritative outcome. When the session was disqualified,
        # the reported violation must be disqualifying too, even if the response we surfaced
        # is an earlier (non-disqualifying) one such as the first 429 of a burst.
        if deposit.get("deposit_state") == "FORFEITED" and not violation.disqualifying:
            violation.disqualifying = True
            violation.deposit_consequence = deposit.get("consequence")
        return {
            "outcome": "refused",
            "violation": violation,
            "deposit": deposit,
        }

    def _complete_and_settle(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        token_headers = {**headers, "Authorization": f"Bearer {session['token']}"}
        complete = http.post("/sessions/complete", headers=token_headers)
        run.emit(
            "Complete the session and settle the deposit",
            request="POST /sessions/complete",
            http_status=complete.status_code,
            reason_code=_reason(complete),
            allowed=complete.status_code == 200,
            message=_message(complete) or "The gateway evaluates the audit trail and returns or keeps the deposit.",
        )
        details = http.get(f"/deposits/{session['deposit_id']}", headers=token_headers)
        if details.status_code != 200:
            return {"deposit_state": None, "consequence": "unknown (could not read the deposit)"}
        body = details.json()
        state = body.get("deposit_state")
        if state == "FORFEITED":
            consequence = "kept by the publisher (forfeited)"
        elif state in ("REFUNDED", "RELEASED"):
            consequence = "returned to the agent"
        else:
            consequence = "still held"
        return {
            "deposit_id": body.get("deposit_id"),
            "amount": body.get("required_amount"),
            "currency": body.get("currency"),
            "deposit_state": state,
            "payment_status": body.get("payment_status"),
            "policy_decision": body.get("policy_decision"),
            "refund_status": body.get("refund_status"),
            "consequence": consequence,
        }

    def _pause(self, run: PlaygroundRun, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = run.deadline_monotonic
        if deadline is not None:
            # Never sleep past the run deadline; the guard below turns that into a clear error.
            seconds = min(seconds, max(0.0, deadline - self._clock()))
        # Sleep in small slices so a cancel responds promptly.
        remaining = seconds
        while remaining > 0 and not run.cancel_event.is_set():
            slice_seconds = min(remaining, 0.5)
            self._sleep(slice_seconds)
            remaining -= slice_seconds
        self._check_deadline(run)

    def _check_deadline(self, run: PlaygroundRun) -> Optional[float]:
        """Raise once the run is out of time; otherwise return the seconds left."""
        deadline = run.deadline_monotonic
        if deadline is None:
            return None
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise PlaygroundTimeoutError(
                f"The playground run exceeded its {self._run_timeout_seconds:g}s time limit and was stopped."
            )
        return remaining


def _reason(resp: Any) -> Optional[str]:
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("reason_code") or body.get("error")
    return str(code).upper() if code else None


def _message(resp: Any) -> Optional[str]:
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body.get("message") or body.get("reason") or body.get("error")


def _retry_after(resp: Any) -> float:
    try:
        return max(0.0, float(resp.headers.get("retry-after") or 0))
    except (TypeError, ValueError):
        return 0.0


def _pace_seconds(policy: dict[str, Any]) -> float:
    window = float(policy.get("rate_limit_window_seconds") or 10.0)
    limit = max(1, int(policy.get("rate_limit_requests") or 3))
    return min(MAX_INTER_REQUEST_PAUSE_SECONDS, max(0.05, window / limit + 0.05))


class _DeadlineHttp:
    """Guards every gateway call with the run deadline."""

    def __init__(self, client: Any, guard: Callable[[], Optional[float]]) -> None:
        self._client = client
        self._guard = guard

    def get(self, url: str, **kwargs: Any) -> Any:
        self._guard()
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        self._guard()
        return self._client.post(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@dataclass(frozen=True)
class _PagePlan:
    """Deterministic pages derived from the live policy for one playground run."""

    challenge: str
    reads: tuple[str, ...] = ()
    fallback: bool = False

    @property
    def pages(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.challenge, *self.reads)))

    def repeat(self, count: int) -> list[str]:
        """`count` page paths, repeating the usable ones when the site has fewer."""
        pages = list(self.pages) or [DEFAULT_PAGE_PATH]
        return [pages[index % len(pages)] for index in range(max(0, count))]


def _normalize_policy_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or "*" in path or "$" in path or "?" in path or "#" in path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


def _path_matches(path: str, rule: str) -> bool:
    if rule == "/":
        return path == "/"
    return path == rule or path.startswith(rule + "/")


def _is_section_container(path: str, tier_prefixes: list[str]) -> bool:
    """True for paths that only group pricing-tier prefixes (e.g. "/content")."""
    return any(prefix != path and prefix.startswith(path + "/") for prefix in tier_prefixes)


def _page_plan(policy: dict[str, Any]) -> _PagePlan:
    """Derive the pages a playground action visits from the current live policy.

    Preference order: advertised allowed paths, concrete pricing-tier resources, tier
    prefixes, then the site root. Reads are limited to the tiers the challenge deposit
    covers, so the deposit never looks insufficient for its own demo. `/news` is only the
    last-resort fallback for a policy with no usable paths.
    """
    allowed: list[str] = []
    for raw in policy.get("allowed_paths") or []:
        path = _normalize_policy_path(raw)
        if path and path not in allowed:
            allowed.append(path)
    disallowed: list[str] = []
    for raw in policy.get("disallowed_paths") or []:
        path = _normalize_policy_path(raw)
        if path and path not in disallowed:
            disallowed.append(path)

    tiers: list[dict[str, Any]] = []
    for raw in policy.get("pricing_tiers") or []:
        if not isinstance(raw, dict):
            continue
        prefixes: list[str] = []
        for prefix_raw in [raw.get("path_prefix")] + list(raw.get("additional_path_prefixes") or []):
            prefix = _normalize_policy_path(prefix_raw)
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
        resources: list[str] = []
        for resource in raw.get("resources") or []:
            resource_path = _normalize_policy_path(resource.get("path") if isinstance(resource, dict) else resource)
            if resource_path and resource_path not in resources:
                resources.append(resource_path)
        tiers.append({"rank": int(raw.get("rank") or 1), "prefixes": prefixes, "resources": resources})

    tier_prefixes = [prefix for tier in tiers for prefix in tier["prefixes"]]

    def blocked(path: str) -> bool:
        return any(_path_matches(path, rule) for rule in disallowed)

    def tier_rank(path: str) -> int:
        ranks = [
            tier["rank"]
            for tier in tiers
            if any(_path_matches(path, prefix) for prefix in tier["prefixes"])
        ]
        return max(ranks) if ranks else 0

    ordered: list[str] = []

    def add(path: Optional[str]) -> None:
        if path and path != "/" and not blocked(path) and path not in ordered:
            ordered.append(path)

    for path in allowed:
        if _is_section_container(path, tier_prefixes):
            continue
        add(path)
    for tier in tiers:
        for resource in tier["resources"]:
            add(resource)
    for prefix in tier_prefixes:
        add(prefix)
    if "/" in allowed and not blocked("/"):
        ordered.append("/")

    if not ordered:
        return _PagePlan(DEFAULT_PAGE_PATH, (DEFAULT_PAGE_PATH,), fallback=True)

    challenge = ordered[0]
    covered_rank = tier_rank(challenge)
    reads = tuple(path for path in ordered[1:] if tier_rank(path) <= covered_rank)
    return _PagePlan(challenge, reads)


def _agent_response_headers(resp: Any) -> dict[str, str]:
    """Agent-facing SARA headers that the gateway set on this response."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return {}
    found: dict[str, str] = {}
    for name in AGENT_RESPONSE_HEADERS:
        try:
            value = headers.get(name)
        except Exception:  # pragma: no cover - defensive for odd header containers
            value = None
        if value is not None:
            found[name] = str(value)
    return found


def _header_summary(headers: dict[str, str]) -> str:
    parts: list[str] = []
    if "X-SARA-Session-Id" in headers:
        parts.append(f"Session {headers['X-SARA-Session-Id']}")
    if "X-SARA-Remaining-Budget" in headers:
        parts.append(f"remaining budget {headers['X-SARA-Remaining-Budget']} request(s)")
    if "X-SARA-Remaining-Window" in headers:
        parts.append(f"remaining window {headers['X-SARA-Remaining-Window']}")
    if "X-SARA-Pricing-Tier" in headers:
        parts.append(f"{headers['X-SARA-Pricing-Tier']} tier")
    return "; ".join(parts)


def _origin_miss(resp: Any) -> bool:
    """True when the gateway authorized the request but the origin site had no page."""
    if getattr(resp, "status_code", None) != 404:
        return False
    return bool(_agent_response_headers(resp).get("X-SARA-Session-Id"))


def _terminal_reason_code(run: PlaygroundRun) -> str:
    """Audit reason code for a finished run, derived from its actual outcome."""
    if run.violation is not None and run.violation.reason_code:
        return run.violation.reason_code
    if run.outcome == "allowed":
        return "ALLOWED"
    if run.outcome == "refused":
        return "REFUSED"
    if run.outcome == "error":
        return "PLAYGROUND_RUN_ERROR"
    return "PLAYGROUND_RUN"


def _first_disallowed(policy: dict[str, Any]) -> Optional[str]:
    paths = [path for path in (policy.get("disallowed_paths") or []) if isinstance(path, str) and path != "/"]
    return paths[0] if paths else None


def _match_rule(reason: Optional[str], policy: dict[str, Any], action: str) -> str:
    if reason == "DISALLOWED_PATH":
        return f"Disallow: {_first_disallowed(policy) or '/admin'}"
    if reason == "INVALID_PATH":
        return "Path normalization / encoded traversal rule"
    if reason == "RATE_LIMIT_EXCEEDED":
        return (
            f"Rate limit: {policy.get('rate_limit_requests')} requests per "
            f"{policy.get('rate_limit_window_seconds'):g}s"
        )
    if reason == "MAX_REQUESTS_EXCEEDED":
        return f"Session budget: {policy.get('max_requests_per_session')} allowed requests"
    if reason == "PAYMENT_REQUIRED":
        return "Refundable deposit required before access"
    if reason == "INVALID_SIGNATURE":
        return "Web Bot Auth Ed25519 signature verification"
    if reason == "DEPOSIT_NOT_ACTIVE":
        return "Active, verified, policy-accepted deposit"
    if action == "forged_signature":
        return "Web Bot Auth Ed25519 signature verification"
    return reason or "Gateway policy"


def _violation_from_response(
    resp: Any,
    policy: dict[str, Any],
    *,
    action: str,
    deposit: Optional[dict[str, Any]],
) -> PlaygroundViolation:
    reason = _reason(resp)
    disqualifying = False
    try:
        body = resp.json()
        disqualifying = bool(body.get("disqualifying")) if isinstance(body, dict) else False
    except Exception:
        pass
    consequence = None
    if deposit is not None:
        consequence = deposit.get("consequence")
    return PlaygroundViolation(
        rule_matched=_match_rule(reason, policy, action),
        http_status=resp.status_code,
        reason_code=reason,
        agent_reason=_message(resp) or _reason(resp) or f"HTTP {resp.status_code}",
        deposit_consequence=consequence,
        disqualifying=disqualifying,
    )
