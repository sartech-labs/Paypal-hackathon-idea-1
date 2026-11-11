"""Curated crawler playground: guarded good/bad scenarios against the live gateway.

Each scenario runs a fixed sequence of HTTP calls against this same gateway, using the real
deposit, policy, rate-limit, and settlement machinery. The AI agent may only choose from the
operator's selected checklist; it cannot invent paths or skip actions. Every selected action
gets a fresh challenge/deposit/session and auto-completes its own session so the deposit
consequence shown in the console is final.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.core.agent_identity import sign_request
from server.core.quotas import HourlyQuota
from server.core.security import utc_now_iso
from server.schemas import (
    PlaygroundActionInfo,
    PlaygroundActionResult,
    PlaygroundAgentInfo,
    PlaygroundRecentRun,
    PlaygroundRunResponse,
    PlaygroundStep,
    PlaygroundViolation,
)
from server.services.playground_agent import (
    PLAYGROUND_ACTION_TOOL,
    PLAYGROUND_AGENT_SYSTEM,
    build_agent_prompt,
)

logger = logging.getLogger("sara.playground")

MAX_RUNS_KEPT = 20
MAX_HTML_BYTES = 400_000
# A playground run holds the single run slot, so every pause is capped and every run has a
# hard deadline: a long rate-limit window must never make a demo wait for minutes.
MAX_INTER_REQUEST_PAUSE_SECONDS = 2.0
DEFAULT_RUN_TIMEOUT_SECONDS = 60.0
DEFAULT_AGENT_RUN_TIMEOUT_SECONDS = 180.0
DEFAULT_AGENT_MAX_TURNS = 12
MAX_BURST_REQUESTS = 1100
MAX_REDIRECTS = 4
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_ACTIONS = 6
DEFAULT_SELECTED_ACTIONS = ("allowed_page", "disallowed_path")
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

ACTIONS: tuple[dict[str, Any], ...] = (
    {
        "action_id": "allowed_page",
        "label": "Allowed page",
        "tone": "good",
        "description": "Open a session with the required deposit, send a valid paid token, and read an allowed page.",
        "expected": "HTTP 200 with the rendered page; deposit returned.",
        "default_selected": True,
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
        "default_selected": True,
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
    return [PlaygroundActionInfo(**{"default_selected": False, **action}) for action in ACTIONS]


def default_action_ids() -> list[str]:
    return [action["action_id"] for action in ACTIONS if action.get("default_selected")]


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
    selected_action_ids: list[str] = field(default_factory=list)
    driver: str = "scripted"
    action_results: list[PlaygroundActionResult] = field(default_factory=list)
    agent: Optional[PlaygroundAgentInfo] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    steps: list[PlaygroundStep] = field(default_factory=list)
    audit_event_ids: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_monotonic: float = field(default_factory=time.monotonic)
    deadline_monotonic: Optional[float] = None
    # Internal only: lets a per-action run stream its steps into the parent run.
    step_sink: Optional[Callable[[PlaygroundStep], None]] = field(default=None, repr=False, compare=False)

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
            step = PlaygroundStep(
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
            self.steps.append(step)
        sink = self.step_sink
        if sink is not None:
            sink(step)

    def absorb_step(self, step: PlaygroundStep, action_id: str) -> None:
        """Copy a per-action step into the parent timeline with a new sequence number."""
        with self.lock:
            self.steps.append(
                PlaygroundStep(
                    seq=len(self.steps),
                    at=step.at,
                    label=step.label,
                    action_id=action_id,
                    request=step.request,
                    http_status=step.http_status,
                    reason_code=step.reason_code,
                    allowed=step.allowed,
                    message=step.message,
                    detail=dict(step.detail),
                )
            )

    def update(self, **changes: Any) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self, key, value)

    def snapshot(self, after: int = 0) -> PlaygroundRunResponse:
        with self.lock:
            # Do not resend finished pages on every poll; the final snapshot carries them.
            action_results = [
                result
                if self.status != "running"
                else result.model_copy(update={"rendered_html": None})
                for result in self.action_results
            ]
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
                selected_action_ids=list(self.selected_action_ids),
                driver=self.driver,  # type: ignore[arg-type]
                action_results=action_results,
                agent=self.agent,
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
            driver=self.driver,
            action_count=max(1, len(self.selected_action_ids) or 1),
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


class PlaygroundUnavailableError(RuntimeError):
    status_code = 503
    code = "playground_ai_unavailable"


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
        ai_generator: Optional[Any] = None,
        max_actions: int = DEFAULT_MAX_ACTIONS,
        agent_timeout_seconds: float = DEFAULT_AGENT_RUN_TIMEOUT_SECONDS,
        agent_max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    ) -> None:
        self._quota = HourlyQuota(max_runs_per_hour)
        self._http_factory = http_factory or _default_http_factory
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._run_timeout_seconds = max(1.0, float(run_timeout_seconds))
        self._agent_timeout_seconds = max(1.0, float(agent_timeout_seconds))
        self._agent_max_turns = max(1, int(agent_max_turns))
        self._ai_generator = ai_generator
        self._max_actions = max(1, int(max_actions))
        self._runs: dict[str, PlaygroundRun] = {}
        self._order: list[str] = []
        self._active: Optional[str] = None
        self._lock = threading.Lock()

    def ai_info(self) -> PlaygroundAgentInfo:
        """Describe whether the guarded agent can run here, and with which model."""
        generator = self._ai_generator
        available = False
        provider: Optional[str] = None
        model: Optional[str] = None
        if generator is not None:
            try:
                available = bool(generator.is_available())
                provider = getattr(generator, "provider", None)
                model = getattr(generator, "model", None)
            except Exception:  # a missing/invalid SDK must not break the actions endpoint
                logger.exception("Could not inspect the playground AI provider")
        return PlaygroundAgentInfo(
            available=available,
            driver="agent" if available else "scripted",
            provider=provider if available else None,
            model=model if available else None,
            status="ready" if available else "unavailable",
        )

    @property
    def max_actions(self) -> int:
        return self._max_actions

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
        action_id: Optional[str] = None,
        action_ids: Optional[list[str]] = None,
        driver: Optional[str] = None,
        base_url: str,
        policy: dict[str, Any],
        audit: Optional[Callable[[str, str, dict[str, Any]], Optional[str]]] = None,
        policy_version: Optional[str] = None,
        payment_mode: Optional[str] = None,
        site_id: Optional[str] = None,
    ) -> PlaygroundRun:
        requested = [str(value).strip() for value in (action_ids or [])]
        if action_id is not None:
            if requested:
                raise PlaygroundActionError("Send either action_id or action_ids, not both.")
            requested = [str(action_id).strip()]
        if not requested:
            raise PlaygroundActionError("Select at least one playground action.")
        if len(requested) > self._max_actions:
            raise PlaygroundActionError(
                f"Select at most {self._max_actions} actions per run."
            )
        if len(set(requested)) != len(requested):
            raise PlaygroundActionError("Each selected action may appear only once.")
        for selected_id in requested:
            if selected_id not in ACTIONS_BY_ID:
                raise PlaygroundActionError(f"Unknown playground action: {selected_id}")

        # The old single-action API keeps its scripted behaviour. The checklist is agent-only,
        # so a deployment without an AI key disables the run instead of silently scripting it.
        legacy = action_id is not None and action_ids is None
        run_driver = driver or ("scripted" if legacy else "agent")
        if run_driver not in ("agent", "scripted"):
            raise PlaygroundActionError(f"Unknown playground driver: {run_driver}")
        if run_driver == "agent" and not self.ai_info().available:
            raise PlaygroundUnavailableError(
                "The agentic playground needs an AI key. Set OPENAI_API_KEY or "
                "ANTHROPIC_API_KEY on the gateway to enable runs."
            )

        actions = [ACTIONS_BY_ID[selected_id] for selected_id in requested]
        label = actions[0]["label"] if len(actions) == 1 else f"{len(actions)} selected actions"
        tone = "bad" if any(action["tone"] == "bad" for action in actions) else "good"
        timeout = (
            self._run_timeout_seconds
            if run_driver == "scripted" and len(requested) == 1
            else self._agent_timeout_seconds
        )
        with self._lock:
            active = self._runs.get(self._active) if self._active else None
            if active is not None and not active.finished:
                raise PlaygroundBusyError("A playground run is already in progress.")
            if not self._quota.try_acquire():
                raise PlaygroundQuotaError("The hourly playground limit was reached. Try again later.")
            run = PlaygroundRun(
                run_id=f"pg_{secrets.token_hex(6)}",
                action_id=requested[0],
                label=label,
                tone=tone,
                selected_action_ids=list(requested),
                driver=run_driver,
                deadline_monotonic=self._clock() + timeout,
            )
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            while len(self._order) > MAX_RUNS_KEPT:
                self._runs.pop(self._order.pop(0), None)
            self._active = run.run_id

        thread = threading.Thread(
            target=self._execute,
            args=(run, base_url, policy, audit, policy_version, payment_mode, requested, run_driver, site_id),
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
        action_ids: list[str],
        driver: str,
        site_id: Optional[str] = None,
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
            {
                "run_id": run.run_id,
                "action_id": run.action_id,
                "selected_action_ids": list(action_ids),
                "label": run.label,
                "tone": run.tone,
                "driver": driver,
            },
        )
        headers = {
            # A non-browser User-Agent is what marks playground traffic as an agent.
            "User-Agent": "SARA-Playground/1.0",
            "X-SARA-Playground-Run": run.run_id,
        }
        if site_id:
            # Exercise the console's selected site, not whichever site is globally active.
            headers["X-SARA-Site"] = site_id
        try:
            with self._http_factory(base_url) as raw_http:
                http = _DeadlineHttp(raw_http, lambda: self._check_deadline(run))
                self._execute_selected(
                    run, http, headers, policy, policy_version, payment_mode, action_ids, driver, log
                )
        except PlaygroundTimeoutError as exc:
            logger.warning("Playground run %s hit its time limit", run.run_id)
            run.emit(
                "Run stopped by the playground time limit",
                message=str(exc),
                detail={"time_limit_seconds": self._agent_timeout_seconds},
            )
            run.update(status="failed", outcome="error", error=str(exc), finished_at=utc_now_iso())
        except Exception as exc:
            logger.exception("Playground run %s failed", run.run_id)
            run.update(status="failed", outcome="error", error=f"{type(exc).__name__}: {exc}", finished_at=utc_now_iso())
            run.emit("Run failed", message=run.error, detail={"error": type(exc).__name__})
        else:
            changes = self._aggregate_results(run)
            if run.cancel_event.is_set():
                changes["status"] = "cancelled"
            elif any(result.status == "failed" for result in changes.get("_results", [])):
                changes["status"] = "failed"
                errors = [result.error for result in changes.get("_results", []) if result.error]
                if errors:
                    changes["error"] = errors[0]
            else:
                changes["status"] = "completed"
            changes.pop("_results", None)
            run.update(finished_at=utc_now_iso(), **changes)
        finally:
            run.update(elapsed_ms=int((time.monotonic() - run.started_monotonic) * 1000))
            log(
                "PLAYGROUND_RUN_FINISHED",
                _terminal_reason_code(run),
                {
                    "run_id": run.run_id,
                    "action_id": run.action_id,
                    "selected_action_ids": list(action_ids),
                    "driver": driver,
                    "outcome": run.outcome,
                    "status": run.status,
                    "deposit_state": (run.deposit or {}).get("deposit_state"),
                    "reason_code": run.violation.reason_code if run.violation else None,
                    "action_outcomes": {
                        result.action_id: result.outcome for result in run.action_results
                    },
                },
            )

    def _execute_selected(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        policy_version: Optional[str],
        payment_mode: Optional[str],
        action_ids: list[str],
        driver: str,
        log: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        if driver == "agent":
            self._run_agent(run, http, headers, policy, policy_version, payment_mode, action_ids, log)
            return
        for action_id in action_ids:
            if run.cancel_event.is_set():
                break
            self._perform_action(
                run, action_id, http, headers, policy, policy_version, payment_mode, log
            )

    def _perform_action(
        self,
        parent: PlaygroundRun,
        action_id: str,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        policy_version: Optional[str],
        payment_mode: Optional[str],
        log: Optional[Callable[[str, str, dict[str, Any]], None]] = None,
    ) -> PlaygroundActionResult:
        """Run one selected scenario with its own challenge, deposit, and session."""
        action = ACTIONS_BY_ID[action_id]
        child = PlaygroundRun(
            run_id=parent.run_id,
            action_id=action_id,
            label=action["label"],
            tone=action["tone"],
            cancel_event=parent.cancel_event,
            started_monotonic=time.monotonic(),
            deadline_monotonic=parent.deadline_monotonic,
            step_sink=lambda step: parent.absorb_step(step, action_id),
        )
        try:
            result = self._dispatch(child, http, headers, policy, policy_version, payment_mode)
        except PlaygroundTimeoutError as exc:
            child.emit("Action stopped by the playground time limit", message=str(exc))
            child.update(status="failed", outcome="error", error=str(exc), finished_at=utc_now_iso())
        except Exception as exc:
            logger.exception("Playground action %s failed in run %s", action_id, parent.run_id)
            child.update(
                status="failed",
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now_iso(),
            )
            child.emit("Action failed", message=child.error, detail={"error": type(exc).__name__})
        else:
            child.update(status="completed", finished_at=utc_now_iso(), **result)
        finally:
            child.update(elapsed_ms=int((time.monotonic() - child.started_monotonic) * 1000))

        action_result = _action_result_from_run(child)
        with parent.lock:
            parent.action_results.append(action_result)
        if log is not None:
            log(
                "PLAYGROUND_ACTION_FINISHED",
                _terminal_reason_code(child),
                {
                    "run_id": parent.run_id,
                    "action_id": action_id,
                    "outcome": child.outcome,
                    "status": child.status,
                    "deposit_state": (child.deposit or {}).get("deposit_state"),
                    "reason_code": child.violation.reason_code if child.violation else None,
                },
            )
        return action_result

    def _run_agent(
        self,
        run: PlaygroundRun,
        http: Any,
        headers: dict[str, str],
        policy: dict[str, Any],
        policy_version: Optional[str],
        payment_mode: Optional[str],
        action_ids: list[str],
        log: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        generator = self._ai_generator
        if generator is None:
            raise PlaygroundUnavailableError("No AI generator is configured for the playground.")
        info = self.ai_info()
        run.update(agent=info.model_copy(update={"status": "running"}))
        run.emit(
            "Agent received the selected checklist",
            message=", ".join(ACTIONS_BY_ID[action_id]["label"] for action_id in action_ids),
            detail={"agent": True, "tools": ["run_action"], "action_ids": list(action_ids)},
        )
        completed: set[str] = set()

        async def execute(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
            if name != "run_action":
                return json.dumps({"error": f"Unknown tool: {name}"}), True
            action_id = str(arguments.get("action_id") or "").strip()
            if action_id not in action_ids:
                return (
                    json.dumps(
                        {
                            "error": (
                                f"Action {action_id!r} is not in the selected checklist. "
                                f"Use one of: {', '.join(action_ids)}."
                            )
                        }
                    ),
                    True,
                )
            if action_id in completed:
                return json.dumps({"error": f"Action {action_id!r} already ran in this checklist."}), True
            if run.cancel_event.is_set():
                return json.dumps({"error": "The run was cancelled."}), True
            action = ACTIONS_BY_ID[action_id]
            run.emit(
                f"Agent chose: {action['label']}",
                message=action["description"],
                detail={"agent": True, "tool": "run_action", "action_id": action_id},
            )
            result = self._perform_action(
                run, action_id, http, headers, policy, policy_version, payment_mode, log
            )
            completed.add(action_id)
            payload = _action_tool_payload(result)
            run.emit(
                f"Agent observed: {action['label']} → {result.outcome or result.status}",
                message=_action_observation(result),
                http_status=(result.violation.http_status if result.violation else (200 if result.outcome == "allowed" else None)),
                reason_code=result.violation.reason_code if result.violation else None,
                allowed=result.outcome == "allowed" if result.outcome else None,
                detail={"agent": True, "action_id": action_id, "result": payload},
            )
            return json.dumps(payload, indent=2), result.status == "failed"

        async def run_loop() -> Any:
            deadline = run.deadline_monotonic
            timeout = None
            if deadline is not None:
                timeout = max(1.0, deadline - self._clock())
            coroutine = generator.run_tool_loop(
                system=PLAYGROUND_AGENT_SYSTEM,
                prompt=build_agent_prompt(
                    [ACTIONS_BY_ID[action_id] for action_id in action_ids], policy
                ),
                tools=[PLAYGROUND_ACTION_TOOL],
                execute=execute,
                max_steps=self._agent_max_turns,
                max_tokens=16000,
            )
            if timeout is None:
                return await coroutine
            try:
                return await asyncio.wait_for(coroutine, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise PlaygroundTimeoutError(
                    f"The playground run exceeded its {self._agent_timeout_seconds:g}s time limit and was stopped."
                ) from exc

        loop_result = asyncio.run(run_loop())
        summary = (loop_result.text or "").strip() or _fallback_summary(run, action_ids)
        run.update(
            agent=info.model_copy(
                update={
                    "status": "completed",
                    "steps": loop_result.steps,
                    "tool_calls": loop_result.tool_calls,
                    "model": loop_result.model or info.model,
                    "summary": summary,
                }
            )
        )
        run.emit("Agent summary", message=summary, detail={"agent": True, "summary": True})

        remaining = [action_id for action_id in action_ids if action_id not in completed]
        if remaining and not run.cancel_event.is_set():
            run.emit(
                "Agent stopped before the full checklist",
                message=(
                    "The playground is completing the remaining selected actions so the "
                    "checklist is never left half-run."
                ),
                detail={"agent": True, "remaining_action_ids": remaining},
            )
            for action_id in remaining:
                if run.cancel_event.is_set():
                    break
                self._perform_action(
                    run, action_id, http, headers, policy, policy_version, payment_mode, log
                )

    def _aggregate_results(self, run: PlaygroundRun) -> dict[str, Any]:
        with run.lock:
            results = list(run.action_results)
        if not results:
            return {"outcome": "error", "error": "No playground action completed.", "_results": results}
        outcomes = {result.outcome for result in results}
        if any(result.status == "failed" for result in results):
            outcome = "error"
        elif outcomes == {"allowed"}:
            outcome = "allowed"
        elif outcomes == {"refused"}:
            outcome = "refused"
        else:
            outcome = "mixed"
        last = results[-1]
        return {
            "outcome": outcome,
            "rendered_html": last.rendered_html,
            "content_type": last.content_type,
            "violation": last.violation,
            "deposit": last.deposit,
            "session_id": last.session_id,
            "_results": results,
        }

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
            headers=headers,
        )
        if deposit.status_code != 201:
            raise PlaygroundActionError(
                f"Deposit creation failed with HTTP {deposit.status_code}: {_message(deposit)}"
            )
        deposit_body = deposit.json()
        deposit_id = deposit_body["deposit_id"]
        ids = {"crawler_id": crawler_id, "challenge_id": challenge_id}
        capture = http.post(f"/deposits/{deposit_id}/capture", json=ids, headers=headers)
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
        verify = http.post(f"/deposits/{deposit_id}/verify", json=ids, headers=headers)
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
            headers=headers,
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

        A same-site redirect (for example `/pages` -> `/pages/`) is also not a policy
        refusal. The reader follows it through the gateway, never directly to the origin,
        so the redirect target is still checked against the live policy.
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
            followed = 0
            redirect: Optional[str] = None
            resp: Any = None
            miss = False
            while True:
                resp = http.get(path, headers=token_headers)
                miss = _origin_miss(resp) or resp.status_code in REDIRECT_STATUSES
                redirect = (
                    _redirect_location(resp, path)
                    if resp.status_code in REDIRECT_STATUSES
                    else None
                )
                total = max(len(paths), index + len(queue))
                note = None
                if redirect is not None:
                    note = (
                        f"The gateway authorized GET {path}, and the origin answered HTTP "
                        f"{resp.status_code} with a redirect to {redirect}. The playground "
                        "follows it through the gateway so policy still applies."
                    )
                elif miss:
                    note = (
                        f"The gateway authorized GET {path}, but the site origin returned HTTP "
                        f"{resp.status_code} without a usable page. This is a content gap, not "
                        "a policy refusal."
                    )
                self._record_response(
                    run,
                    f"{label} {index}/{total}",
                    f"GET {path}",
                    resp,
                    include_body=resp.status_code == 200,
                    note=note,
                )
                if redirect is None:
                    break
                followed += 1
                if followed > MAX_REDIRECTS:
                    miss = True
                    break
                path = redirect
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
        # A site with no advertised paths (a freshly added origin site) is best exercised at
        # its root; the bundled demo policy falls back to /news. Prefer an unblocked
        # candidate, but when the site forbids every candidate there is no usable path.
        candidates = ["/", DEFAULT_PAGE_PATH] if not allowed and not tiers else [DEFAULT_PAGE_PATH]
        fallback = next((path for path in candidates if not blocked(path)), candidates[0])
        return _PagePlan(fallback, (fallback,), fallback=True)

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


def _redirect_location(resp: Any, current_path: str) -> Optional[str]:
    """Return a gateway-relative path for an origin redirect, ignoring its host.

    Origin sites often redirect to an absolute URL such as
    `http://www.example.com/pages/`. Following that URL directly would bypass the
    gateway, so only the path and query are kept and re-requested through SARA.
    """
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    location = headers.get("location")
    if not location:
        return None
    target = urlsplit(str(location))
    if target.scheme or target.netloc:
        path = target.path or "/"
        query = target.query
    else:
        resolved = urlsplit(urljoin(current_path, str(location)))
        path = resolved.path or "/"
        query = resolved.query
    if not path.startswith("/"):
        path = "/" + path
    return f"{path}?{query}" if query else path


def _action_result_from_run(run: PlaygroundRun) -> PlaygroundActionResult:
    """Freeze one per-action run into the result returned to the browser."""
    with run.lock:
        return PlaygroundActionResult(
            action_id=run.action_id,
            label=run.label,
            tone=run.tone,  # type: ignore[arg-type]
            status=run.status,  # type: ignore[arg-type]
            outcome=run.outcome,  # type: ignore[arg-type]
            rendered_html=run.rendered_html,
            content_type=run.content_type,
            violation=run.violation,
            deposit=dict(run.deposit) if run.deposit else None,
            session_id=run.session_id,
            error=run.error,
            elapsed_ms=run.elapsed_ms,
            steps=list(run.steps),
        )


def _action_tool_payload(result: PlaygroundActionResult) -> dict[str, Any]:
    """Small, model-safe view of an action result (never the page HTML or tokens)."""
    violation = result.violation
    deposit = result.deposit or {}
    return {
        "action_id": result.action_id,
        "label": result.label,
        "outcome": result.outcome,
        "status": result.status,
        "http_status": (
            violation.http_status
            if violation is not None
            else (200 if result.outcome == "allowed" else None)
        ),
        "reason_code": violation.reason_code if violation else None,
        "rule_matched": violation.rule_matched if violation else None,
        "agent_reason": violation.agent_reason if violation else None,
        "deposit_state": deposit.get("deposit_state"),
        "deposit_consequence": (
            (violation.deposit_consequence if violation else None)
            or deposit.get("consequence")
        ),
        "session_id": result.session_id,
        "page_returned": bool(result.rendered_html),
        "content_type": result.content_type,
        "error": result.error,
    }


def _action_observation(result: PlaygroundActionResult) -> str:
    violation = result.violation
    deposit = result.deposit or {}
    if result.error:
        return result.error
    if result.outcome == "allowed":
        return (
            "The gateway allowed the action."
            + (f" Deposit: {deposit.get('consequence')}." if deposit.get("consequence") else "")
        )
    if result.outcome == "refused":
        parts = [f"HTTP {violation.http_status}." if violation and violation.http_status else "The gateway refused the action."]
        if violation and violation.reason_code:
            parts.append(f"Reason: {violation.reason_code}.")
        if violation and violation.rule_matched:
            parts.append(f"Rule: {violation.rule_matched}.")
        if deposit.get("consequence"):
            parts.append(f"Deposit: {deposit.get('consequence')}.")
        return " ".join(parts)
    return "The action did not complete."


def _fallback_summary(run: PlaygroundRun, action_ids: list[str]) -> str:
    with run.lock:
        results = list(run.action_results)
    if not results:
        return "The agent did not run any selected actions before stopping."
    lines = [f"The agent ran {len(results)} of {len(action_ids)} selected action(s):"]
    for result in results:
        deposit = result.deposit or {}
        line = f"- {result.label}: {result.outcome or result.status}"
        if result.violation and result.violation.reason_code:
            line += f" ({result.violation.reason_code})"
        if deposit.get("consequence"):
            line += f"; deposit {deposit['consequence']}"
        if result.error:
            line += f"; error: {result.error}"
        lines.append(line)
    return "\n".join(lines)


def _terminal_reason_code(run: PlaygroundRun) -> str:
    """Audit reason code for a finished run, derived from its actual outcome."""
    if run.outcome == "mixed":
        return "PLAYGROUND_MIXED"
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
