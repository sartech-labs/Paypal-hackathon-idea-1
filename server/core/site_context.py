"""Which website the current request is about.

The container records the site it resolved for a request here, and the audit logger stamps
that site on every event the request writes. Each request runs in its own context, so
concurrent requests for different websites never see each other's site.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

current_site_id: ContextVar[Optional[str]] = ContextVar("sara_current_site_id", default=None)


def set_current_site(site_id: Optional[str]) -> None:
    current_site_id.set(site_id or None)


def get_current_site() -> Optional[str]:
    return current_site_id.get()
