"""Attribute deposits, sessions, and audit events to websites, and filter by website.

Deposits carry the site they were taken for. Sessions and audit events carry it when they
were written by a request that resolved a site; for older records the site is recovered
through the deposit or session they belong to. A record whose site is unknown, or whose
site has since been removed, belongs to no current website and is left out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from server.schemas import AuditEvent, DepositRecord, SessionRecord


@dataclass
class SiteAttribution:
    deposit_sites: dict[str, str]
    session_sites: dict[str, str]

    @classmethod
    def build(cls, deposits: Iterable[DepositRecord], sessions: Iterable[SessionRecord]) -> "SiteAttribution":
        deposit_sites = {d.deposit_id: d.site_id for d in deposits if d.site_id}
        session_sites: dict[str, str] = {}
        for s in sessions:
            site = s.site_id or deposit_sites.get(s.deposit_id, "")
            if site:
                session_sites[s.session_id] = site
        return cls(deposit_sites=deposit_sites, session_sites=session_sites)

    def session_site(self, session: SessionRecord) -> Optional[str]:
        return self.session_sites.get(session.session_id)

    def event_site(self, event: AuditEvent) -> Optional[str]:
        if event.site_id:
            return event.site_id
        if event.deposit_id and event.deposit_id in self.deposit_sites:
            return self.deposit_sites[event.deposit_id]
        if event.session_id and event.session_id in self.session_sites:
            return self.session_sites[event.session_id]
        return None


@dataclass
class EventCounts:
    allowed: int = 0
    rejected: int = 0
    violations: int = 0
    violations_by_reason: dict[str, int] | None = None


GATEWAY_DECISIONS = ("GATEWAY_ACCESS_ALLOWED", "GATEWAY_ACCESS_REJECTED", "POLICY_VIOLATION")


def count_events(events: Iterable[AuditEvent]) -> EventCounts:
    """Gateway request and violation counts, as the audit summary computes them."""
    counts = EventCounts(violations_by_reason={})
    for ev in events:
        if ev.event_type in GATEWAY_DECISIONS:
            if ev.allowed is True:
                counts.allowed += 1
            else:
                counts.rejected += 1
        if ev.event_type == "POLICY_VIOLATION":
            counts.violations += 1
            reason = ev.reason_code or "UNKNOWN"
            counts.violations_by_reason[reason] = counts.violations_by_reason.get(reason, 0) + 1
    return counts
