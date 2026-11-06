"""Live demo runner: starts a crawler from the web UI and records what happens.

A run drives the same GatewayToolbox as the command-line agent, over HTTP against this
gateway, in a background thread. The page polls the run for new events. Two drivers:
- ai: the AI crawler agent (OpenAI or Claude, whichever the gateway is configured with)
- scripted: fixed decisions, no AI key needed

Guards for public demos: one run at a time, an hourly cap, a turn limit, a spending
limit enforced in code, and runs that time out instead of waiting forever.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx

from client.agent import PERSONAS, CrawlerAgent
from client.agent_tools import GatewayToolbox
from client.scripted_agent import ScriptedDriver
from server.core.quotas import HourlyQuota
from server.core.security import utc_now_iso
from server.schemas import DemoRunEvent, DemoRunResponse, PricingTier

logger = logging.getLogger("crawlertrust.demo")

# Default operator spending limit enforced by the crawler's toolbox. The gateway raises it
# to its highest published price so every configured tier stays payable.
MAX_DEPOSIT = Decimal("5.00")
MAX_TURNS = 25
APPROVAL_TIMEOUT_SECONDS = 600.0
APPROVAL_POLL_SECONDS = 2.0
MAX_RUNS_KEPT = 20
MAX_CONTENT_PREVIEW = 700

HttpFactory = Callable[[str], AbstractContextManager[Any]]
LlmFactory = Callable[[str], Any]


class DemoRunError(RuntimeError):
    status_code = 400
    code = "demo_run_error"


class DemoBusyError(DemoRunError):
    status_code = 409
    code = "demo_run_in_progress"


class DemoQuotaError(DemoRunError):
    status_code = 429
    code = "demo_run_quota_exceeded"


class DemoUnavailableError(DemoRunError):
    status_code = 503
    code = "demo_ai_unavailable"


def _default_http_factory(base_url: str) -> AbstractContextManager[Any]:
    return httpx.Client(base_url=base_url, timeout=30.0)


def _default_llm_factory(provider: str) -> Any:
    if provider == "openai":
        import openai

        return openai.OpenAI(max_retries=2, timeout=120.0)
    import anthropic

    return anthropic.Anthropic(max_retries=2, timeout=120.0)


@dataclass
class DemoRun:
    run_id: str
    driver: str
    persona: str
    crawler_id: str
    provider: Optional[str]
    model: Optional[str]
    status: str = "running"
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: Optional[str] = None
    approval_url: Optional[str] = None
    deposit: Optional[dict[str, Any]] = None
    session: Optional[dict[str, Any]] = None
    explanation: Optional[dict[str, Any]] = None
    final_text: Optional[str] = None
    error: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    pricing_tier: Optional[dict[str, Any]] = None
    events: list[DemoRunEvent] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    approved_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def emit(self, kind: str, data: Optional[dict[str, Any]] = None) -> None:
        with self.lock:
            self.events.append(DemoRunEvent(seq=len(self.events), at=utc_now_iso(), kind=kind, data=data or {}))

    def update(self, **changes: Any) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self, key, value)

    def snapshot(self, after: int = 0) -> DemoRunResponse:
        with self.lock:
            return DemoRunResponse(
                run_id=self.run_id,
                driver=self.driver,  # type: ignore[arg-type]
                persona=self.persona,  # type: ignore[arg-type]
                status=self.status,  # type: ignore[arg-type]
                crawler_id=self.crawler_id,
                model=self.model,
                provider=self.provider,
                started_at=self.started_at,
                finished_at=self.finished_at,
                approval_url=self.approval_url,
                deposit=self.deposit,
                session=self.session,
                explanation=self.explanation,
                final_text=self.final_text,
                error=self.error,
                usage=dict(self.usage),
                pricing_tier=dict(self.pricing_tier) if self.pricing_tier else None,
                events=[event for event in self.events if event.seq >= after],
                next_seq=len(self.events),
            )

    def summary(self) -> dict[str, Any]:
        with self.lock:
            deposit = self.deposit or {}
            return {
                "run_id": self.run_id,
                "driver": self.driver,
                "persona": self.persona,
                "status": self.status,
                "crawler_id": self.crawler_id,
                "started_at": self.started_at,
                "deposit_id": deposit.get("deposit_id"),
                "deposit_state": deposit.get("deposit_state"),
                "pricing_tier": (self.pricing_tier or {}).get("tier_id"),
            }


def _tier_summary(tier: PricingTier) -> dict[str, Any]:
    resources = [resource.path for resource in tier.resources]
    return {
        "tier_id": tier.tier_id,
        "name": tier.name,
        "price": tier.price,
        "currency": tier.currency,
        "resource": resources[0] if resources else tier.path_prefix,
        "resources": resources or [tier.path_prefix],
    }


def _tool_result_event(name: str, result: Any, is_error: bool) -> dict[str, Any]:
    """Trim tool results for the page: page text is shown as a short preview."""
    data = dict(result) if isinstance(result, dict) else {"value": result}
    content = data.get("content")
    if isinstance(content, str):
        text = content.replace("<untrusted_page_content>", "").replace("</untrusted_page_content>", "").strip()
        data["content"] = text[:MAX_CONTENT_PREVIEW] + ("..." if len(text) > MAX_CONTENT_PREVIEW else "")
        data["prompt_injection"] = "note to ai agents" in text.lower()
    if isinstance(data.get("robots_txt"), str):
        data["robots_txt"] = data["robots_txt"][:400]
    return {"name": name, "result": data, "is_error": is_error}


class DemoRunner:
    """Owns demo runs: at most one active at a time, capped per hour."""

    def __init__(
        self,
        *,
        max_runs_per_hour: int,
        http_factory: Optional[HttpFactory] = None,
        llm_factory: Optional[LlmFactory] = None,
        sleep: Optional[Callable[[float], None]] = None,
        max_deposit: Decimal = MAX_DEPOSIT,
    ) -> None:
        self._max_deposit = max_deposit
        self._http_factory = http_factory or _default_http_factory
        self._llm_factory = llm_factory or _default_llm_factory
        self._sleep = sleep  # tests skip real waits; runs otherwise wait (interruptibly)
        self._quota = HourlyQuota(max_runs_per_hour)
        self._runs: dict[str, DemoRun] = {}
        self._order: list[str] = []
        self._active: Optional[str] = None
        self._lock = threading.Lock()

    def runs_remaining(self) -> int:
        return self._quota.remaining()

    def active_run_id(self) -> Optional[str]:
        with self._lock:
            run = self._runs.get(self._active) if self._active else None
            return run.run_id if run is not None and not run.finished else None

    def get(self, run_id: str) -> Optional[DemoRun]:
        with self._lock:
            return self._runs.get(run_id)

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            runs = [self._runs[run_id] for run_id in reversed(self._order[-limit:])]
        return [run.summary() for run in runs]

    def start(
        self,
        *,
        driver: str,
        persona: str,
        base_url: str,
        ai_provider: Optional[str],
        ai_model: Optional[str],
        ai_effort: str = "medium",
        ai_available: bool = False,
        tier: Optional[PricingTier] = None,
    ) -> DemoRun:
        if persona not in PERSONAS:
            raise DemoRunError(f"Unknown persona {persona!r}.")
        if driver == "ai" and not ai_available:
            raise DemoUnavailableError(
                "No AI key is configured on the gateway. Set OPENAI_API_KEY or ANTHROPIC_API_KEY, "
                "or run the scripted crawler."
            )
        provider = None
        if driver == "ai":
            provider = "openai" if ai_provider == "openai" else "anthropic"
        with self._lock:
            active = self._runs.get(self._active) if self._active else None
            if active is not None and not active.finished:
                raise DemoBusyError("A demo run is already in progress. Wait for it to finish or stop it.")
            if not self._quota.try_acquire():
                raise DemoQuotaError("The hourly limit for demo runs was reached. Try again later.")
            prefix = "ai" if driver == "ai" else "scripted"
            run = DemoRun(
                run_id=f"run_{secrets.token_hex(6)}",
                driver=driver,
                persona=persona,
                crawler_id=f"{prefix}-{persona}-{secrets.token_hex(3)}",
                provider=provider,
                model=ai_model if driver == "ai" else "scripted",
                # Tier selection drives the compliant crawler; the rogue scenario is unchanged.
                pricing_tier=_tier_summary(tier) if tier is not None and persona == "compliant" else None,
            )
            self._runs[run.run_id] = run
            self._order.append(run.run_id)
            while len(self._order) > MAX_RUNS_KEPT:
                self._runs.pop(self._order.pop(0), None)
            self._active = run.run_id

        thread = threading.Thread(
            target=self._execute,
            args=(run, base_url, ai_effort),
            name=f"demo-{run.run_id}",
            daemon=True,
        )
        thread.start()
        return run

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.finished:
            return False
        run.cancel_event.set()
        run.approved_event.set()
        return True

    def confirm_approval(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.status != "awaiting_approval":
            return False
        run.approved_event.set()
        return True

    # --- run execution (background thread) -----------------------------------------------

    def _execute(self, run: DemoRun, base_url: str, effort: str) -> None:
        persona = PERSONAS[run.persona]
        tier = run.pricing_tier
        if tier:
            resources = ", ".join(tier["resources"])
            persona = replace(
                persona,
                task=(
                    f"Retrieve this site's {tier['name']} tier content ({resources}), starting with "
                    f"{tier['resource']}, and write a short brief. The site prices content by tier and "
                    "its gateway sets the deposit; pay only what the 402 challenge asks, and only if it "
                    "fits your spending limit."
                ),
            )
        run.emit(
            "run_started",
            {
                "driver": run.driver,
                "persona": run.persona,
                "persona_label": persona.label,
                "task": persona.task,
                "crawler_id": run.crawler_id,
                "model": run.model,
                "provider": run.provider,
                "pricing_tier": tier,
            },
        )
        try:
            with self._http_factory(base_url) as http:
                toolbox = GatewayToolbox(
                    http=http,
                    crawler_id=run.crawler_id,
                    max_deposit=self._max_deposit,
                    sleep=self._sleep or (lambda seconds: run.cancel_event.wait(seconds)),
                )
                toolbox.approval_handler = lambda url: self._await_approval(run, toolbox, http, url)

                def on_event(kind: str, payload: dict[str, Any]) -> None:
                    if kind == "tool_result":
                        run.emit("tool_result", _tool_result_event(payload["name"], payload["result"], payload["is_error"]))
                        self._sync_state(run, toolbox)
                    elif kind == "tool_call":
                        run.emit("tool_call", {"name": payload["name"], "input": payload["input"]})
                    else:
                        run.emit(kind, dict(payload))

                if run.driver == "ai":
                    assert run.provider is not None
                    agent = CrawlerAgent(
                        llm=self._llm_factory(run.provider),
                        toolbox=toolbox,
                        persona=persona,
                        model=run.model,
                        effort=effort,
                        max_turns=MAX_TURNS,
                        on_event=on_event,
                        provider=run.provider,
                        should_stop=run.cancel_event.is_set,
                    )
                else:
                    agent = CrawlerAgent(
                        llm=None,
                        toolbox=toolbox,
                        persona=persona,
                        model="scripted",
                        max_turns=MAX_TURNS,
                        on_event=on_event,
                        provider="scripted",
                        driver=ScriptedDriver(
                            run.persona,
                            tier_name=tier["name"] if tier else None,
                            tier_resources=tier["resources"] if tier else None,
                        ),
                        should_stop=run.cancel_event.is_set,
                    )
                result = agent.run()
                run.update(
                    usage={
                        "turns": result.turns,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "cached_input_tokens": result.cache_read_tokens,
                    },
                    model=result.model if run.driver == "ai" else "scripted",
                )

                if toolbox.access_token and toolbox.outcome is None:
                    run.emit("text", {"text": "The crawler stopped without completing its session; completing it now so the outcome is recorded."})
                    run.emit("tool_call", {"name": "complete_session", "input": {}})
                    content, is_error = toolbox.execute("complete_session", {})
                    run.emit("tool_result", _tool_result_event("complete_session", json.loads(content), is_error))
                    self._sync_state(run, toolbox)

                explanation = self._fetch_explanation(http, toolbox)
                final_status = "cancelled" if result.stop == "cancelled" else ("failed" if result.stop in ("error", "refusal") else "completed")
                run.update(
                    final_text=result.final_text or None,
                    error=result.error,
                    explanation=explanation,
                    status=final_status,
                    finished_at=utc_now_iso(),
                )
                run.emit("run_finished", {"status": final_status, "stop": result.stop, "error": result.error})
        except Exception as exc:  # report, never crash the server thread silently
            logger.exception("Demo run %s failed", run.run_id)
            run.update(status="failed", error=f"The demo run failed ({type(exc).__name__}).", finished_at=utc_now_iso())
            run.emit("run_finished", {"status": "failed", "error": run.error})

    @staticmethod
    def _sync_state(run: DemoRun, toolbox: GatewayToolbox) -> None:
        changes: dict[str, Any] = {}
        if toolbox.deposit is not None:
            changes["deposit"] = dict(toolbox.deposit)
        if toolbox.session is not None:
            changes["session"] = dict(toolbox.session)
        if toolbox.outcome is not None:
            changes["session"] = dict(toolbox.outcome.get("session") or {})
        if changes:
            run.update(**changes)

    def _await_approval(self, run: DemoRun, toolbox: GatewayToolbox, http: Any, url: str) -> None:
        """Wait for the viewer to approve the PayPal Sandbox checkout in a popup."""
        deposit = toolbox.deposit or {}
        challenge = toolbox.challenge or {}
        run.update(status="awaiting_approval", approval_url=url, deposit=dict(deposit))
        run.emit("approval_required", {"approval_url": url, "deposit_id": deposit.get("deposit_id")})
        deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
        headers = {"X-Crawler-Id": run.crawler_id, "X-Challenge-Id": str(challenge.get("challenge_id", ""))}
        outcome = "timeout"
        while time.monotonic() < deadline and not run.cancel_event.is_set():
            if run.approved_event.wait(APPROVAL_POLL_SECONDS):
                outcome = "confirmed"
                break
            try:
                current = http.get(f"/deposits/{deposit.get('deposit_id')}", headers=headers)
                if current.status_code == 200 and current.json().get("payment_status") in ("CAPTURED", "AUTHORIZED"):
                    outcome = "paid"
                    break
            except (httpx.HTTPError, ValueError):
                continue
        if run.cancel_event.is_set():
            outcome = "cancelled"
        run.update(status="running", approval_url=None)
        run.emit("approval_done", {"outcome": outcome})

    @staticmethod
    def _fetch_explanation(http: Any, toolbox: GatewayToolbox) -> Optional[dict[str, Any]]:
        deposit = toolbox.deposit or {}
        if not toolbox.access_token or not deposit.get("deposit_id"):
            return None
        try:
            response = http.get(
                f"/deposits/{deposit['deposit_id']}/explanation",
                headers={"Authorization": f"Bearer {toolbox.access_token}"},
            )
            return response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
