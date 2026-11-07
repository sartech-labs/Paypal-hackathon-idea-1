"""Saved payment methods: a person consents once, then their agent pays without them present.

The first payment of a task can ask PayPal to save the payment method (Orders v2
`payment_source.<card|paypal>.attributes.vault.store_in_vault = ON_SUCCESS`). PayPal returns a
vault payment token. SARA keeps that token server-side and gives the agent only an opaque
`payment_method_id` (`pm_...`); later payments in the task send that ID and SARA charges the
saved method (`payment_source.<kind>.vault_id`). Card details never reach the agent or the AI.
The ID is a capability: it is unguessable, usable only at this site, and never shown to the model.
"""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any, Optional

from server.core.security import utc_now_iso


class PaymentMethodStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._methods: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self._methods = json.loads(path.read_text(encoding="utf-8")).get("methods", {})
            except (OSError, ValueError):
                self._methods = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"methods": self._methods}, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def add(self, *, kind: str, token: str, label: str, saved_by: str, source: str) -> str:
        pm_id = f"pm_{secrets.token_urlsafe(18)}"
        with self._lock:
            self._methods[pm_id] = {
                "payment_method_id": pm_id, "kind": kind, "token": token, "label": label,
                "saved_by": saved_by, "source": source, "created_at": utc_now_iso(), "uses": 0,
            }
            self._persist()
        return pm_id

    def token_for(self, pm_id: str) -> tuple[str, str]:
        """(kind, vault token) for a saved method; KeyError when unknown."""
        with self._lock:
            m = self._methods[pm_id]
            m["uses"] = int(m.get("uses", 0)) + 1
            self._persist()
            return m["kind"], m["token"]

    def label(self, pm_id: Optional[str]) -> Optional[str]:
        if not pm_id:
            return None
        m = self._methods.get(pm_id)
        return m["label"] if m else None

    def public(self, pm_id: str) -> dict[str, Any]:
        m = self._methods[pm_id]
        return {k: m[k] for k in ("payment_method_id", "kind", "label", "created_at", "uses", "source")}


def resolve_payment_token(store: Optional[PaymentMethodStore], payment_method_id: Optional[str]) -> Optional[str]:
    """'<kind>:<vault token>' for a saved method, ready for create_order; ValueError if unknown."""
    if not payment_method_id:
        return None
    if store is None:
        raise ValueError("Saved payment methods are not enabled on this gateway")
    try:
        kind, token = store.token_for(payment_method_id)
    except KeyError as exc:
        raise ValueError("Unknown payment_method_id") from exc
    return f"{kind}:{token}"


def record_payment_method(
    store: Optional[PaymentMethodStore],
    used_id: Optional[str],
    saved: Optional[dict[str, str]],
    saved_by: str,
    source: str,
) -> tuple[Optional[str], Optional[str]]:
    """(payment_method_id, label) for an order: the saved method it used, or the one it just saved."""
    if used_id and store is not None:
        return used_id, store.label(used_id)
    if saved and store is not None:
        pm_id = store.add(kind=saved["kind"], token=saved["token"], label=saved["label"], saved_by=saved_by, source=source)
        return pm_id, saved["label"]
    return None, None
