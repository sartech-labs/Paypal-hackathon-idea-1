"""Append-only JSONL audit logger and summary aggregator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import ValidationError

from server.core.security import (
    PersistenceCorruptionError,
    sanitize_dict_for_logging,
    utc_now_iso,
)
from server.schemas import (
    AuditEvent,
    AuditSummaryResponse,
    DepositRecord,
    SessionRecord,
)

logger = logging.getLogger("agenttrust.audit")


class AuditLogger:
    """
    Maintains an append-only JSONL audit log on disk and an in-memory mirror
    for fast filtering and summary aggregation.
    """

    def __init__(self, audit_log_path: Path) -> None:
        self._path = audit_log_path
        self._lock = asyncio.Lock()
        self._events: list[AuditEvent] = []

    def load_from_disk(self) -> None:
        """Load and validate existing JSONL audit records on startup."""
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._events = []
            return

        loaded: list[AuditEvent] = []
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                        event = AuditEvent.model_validate(parsed)
                        loaded.append(event)
                    except (json.JSONDecodeError, ValidationError) as exc:
                        raise PersistenceCorruptionError(
                            f"Malformed audit record at {self._path}:{line_number}: {exc}"
                        ) from exc
        except OSError as exc:
            raise PersistenceCorruptionError(
                f"Failed to read audit log {self._path}: {exc}"
            ) from exc

        self._events = loaded

    async def log_event(
        self,
        *,
        event_type: str,
        crawler_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        session_id: Optional[str] = None,
        deposit_id: Optional[str] = None,
        path: Optional[str] = None,
        method: Optional[str] = None,
        allowed: Optional[bool] = None,
        reason_code: Optional[str] = None,
        policy_version: Optional[str] = None,
        payment_mode: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditEvent:
        """Append a sanitized security audit event to disk and memory."""
        safe_details = sanitize_dict_for_logging(details or {})
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=utc_now_iso(),
            event_type=event_type,
            crawler_id=crawler_id,
            challenge_id=challenge_id,
            session_id=session_id,
            deposit_id=deposit_id,
            path=path,
            method=method,
            allowed=allowed,
            reason_code=reason_code,
            policy_version=policy_version,
            payment_mode=payment_mode,
            details=safe_details,
        )

        serialized = json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"

        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            self._events.append(event)

        logger.info(
            "AUDIT event_type=%s crawler_id=%s session_id=%s deposit_id=%s path=%s allowed=%s reason=%s",
            event.event_type,
            event.crawler_id,
            event.session_id,
            event.deposit_id,
            event.path,
            event.allowed,
            event.reason_code,
        )
        return event

    async def get_events(
        self,
        *,
        crawler_id: Optional[str] = None,
        session_id: Optional[str] = None,
        deposit_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 200,
    ) -> list[AuditEvent]:
        """Return filtered audit events in chronological order."""
        async with self._lock:
            results: list[AuditEvent] = []
            for ev in self._events:
                if crawler_id and ev.crawler_id != crawler_id:
                    continue
                if session_id and ev.session_id != session_id:
                    continue
                if deposit_id and ev.deposit_id != deposit_id:
                    continue
                if event_type and ev.event_type != event_type:
                    continue
                results.append(ev.model_copy(deep=True))
            if limit > 0:
                results = results[-limit:]
            return results

    async def build_summary(
        self,
        *,
        deposits: list[DepositRecord],
        sessions: list[SessionRecord],
        scope: Literal["global", "session"] = "global",
        scoped_session_id: Optional[str] = None,
        scoped_crawler_id: Optional[str] = None,
    ) -> AuditSummaryResponse:
        """Aggregate request counts, violations, deposit states, and refund outcomes."""
        async with self._lock:
            events_snapshot = [
                ev
                for ev in self._events
                if (scoped_session_id is None or ev.session_id == scoped_session_id)
                and (scoped_crawler_id is None or ev.crawler_id == scoped_crawler_id)
            ]

        filtered_sessions = [
            s
            for s in sessions
            if (scoped_session_id is None or s.session_id == scoped_session_id)
            and (scoped_crawler_id is None or s.crawler_id == scoped_crawler_id)
        ]
        filtered_deposit_ids = {s.deposit_id for s in filtered_sessions}
        filtered_deposits = [
            d
            for d in deposits
            if (
                scoped_session_id is None
                and (scoped_crawler_id is None or d.crawler_id == scoped_crawler_id)
            )
            or d.deposit_id in filtered_deposit_ids
        ]

        total_gateway = 0
        allowed_gateway = 0
        rejected_gateway = 0
        policy_violations = 0
        disqualifying_violations = 0
        violations_by_reason: dict[str, int] = {}

        for ev in events_snapshot:
            if ev.event_type in ("GATEWAY_ACCESS_ALLOWED", "GATEWAY_ACCESS_REJECTED", "POLICY_VIOLATION"):
                total_gateway += 1
                if ev.allowed is True:
                    allowed_gateway += 1
                else:
                    rejected_gateway += 1
            if ev.event_type == "POLICY_VIOLATION":
                policy_violations += 1
                reason_key = ev.reason_code or "UNKNOWN"
                violations_by_reason[reason_key] = violations_by_reason.get(reason_key, 0) + 1
                if ev.details.get("disqualifying") is True:
                    disqualifying_violations += 1

        deposits_by_state: dict[str, int] = {}
        payments_by_status: dict[str, int] = {}
        decisions_by_status: dict[str, int] = {}
        refunds_by_status: dict[str, int] = {}

        for dep in filtered_deposits:
            st = dep.deposit_state.value
            deposits_by_state[st] = deposits_by_state.get(st, 0) + 1

            ps = dep.payment_status.value
            payments_by_status[ps] = payments_by_status.get(ps, 0) + 1

            pd = dep.policy_decision.value
            decisions_by_status[pd] = decisions_by_status.get(pd, 0) + 1

            rs = dep.refund_status.value
            refunds_by_status[rs] = refunds_by_status.get(rs, 0) + 1

        sessions_by_status: dict[str, int] = {}
        for ses in filtered_sessions:
            ss = ses.status.value
            sessions_by_status[ss] = sessions_by_status.get(ss, 0) + 1

        return AuditSummaryResponse(
            scope=scope,
            scoped_session_id=scoped_session_id,
            scoped_crawler_id=scoped_crawler_id,
            total_events=len(events_snapshot),
            total_gateway_requests=total_gateway,
            allowed_gateway_requests=allowed_gateway,
            rejected_gateway_requests=rejected_gateway,
            policy_violations=policy_violations,
            disqualifying_violations=disqualifying_violations,
            deposits_by_state=deposits_by_state,
            payments_by_status=payments_by_status,
            decisions_by_status=decisions_by_status,
            refunds_by_status=refunds_by_status,
            sessions_by_status=sessions_by_status,
            violation_counts_by_reason=violations_by_reason,
        )

