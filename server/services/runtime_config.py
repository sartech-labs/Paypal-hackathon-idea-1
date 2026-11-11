"""Live gateway configuration: validation, persistence, and origin reachability checks.

Environment variables remain the deployment defaults. A stored runtime config (written by
the owner console) sits on top of them, so the target site, deposit prices, and crawling
rules can change without a redeploy or an env-file edit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import ValidationError

from server.config import Settings
from server.core.security import PersistenceCorruptionError, utc_now_iso
from server.schemas import OriginStatus, RuntimeConfigUpdate
from server.services.origin_proxy import OriginProxy


class StoredRuntimeConfig(RuntimeConfigUpdate):
    """The persisted overlay. `None` means inherit the deployment setting."""

    version: int = 0
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class OriginNotAllowedError(ValueError):
    """A public write tried to point the proxy at a host outside DEMO_ORIGIN_ALLOWLIST."""


class RuntimeConfigStore:
    """Atomic JSON storage for the owner-editable gateway configuration."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> StoredRuntimeConfig:
        if not self._path.exists():
            return StoredRuntimeConfig()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected a JSON object")
            return StoredRuntimeConfig.model_validate(raw)
        except (OSError, ValueError, ValidationError) as exc:
            raise PersistenceCorruptionError(
                f"Malformed gateway configuration at {self._path}: {exc}"
            ) from exc

    def save(self, config: StoredRuntimeConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        # Persist only fields the owner actually set. Writing every default would make a
        # no-op update look like an explicit `origin_url = None` and disable proxying.
        payload = config.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        payload.setdefault("version", config.version)
        if config.updated_at is not None:
            payload.setdefault("updated_at", config.updated_at)
        if config.updated_by is not None:
            payload.setdefault("updated_by", config.updated_by)
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self._path)


def merge_runtime_config(
    current: StoredRuntimeConfig,
    update: RuntimeConfigUpdate,
    *,
    actor: str,
) -> StoredRuntimeConfig:
    """Apply only the fields the caller sent, then stamp the record."""
    data = {
        field: getattr(current, field)
        for field in current.model_fields_set
        if field not in ("version", "updated_at", "updated_by")
        and getattr(current, field) is not None
    }
    for field in update.model_fields_set:
        value = getattr(update, field)
        # An explicit null means "leave the deployment default alone", not "disable it".
        if field == "origin_url" and value is None:
            continue
        if field == "tier_prices" and value is not None:
            value = {str(key): str(price) for key, price in value.items()}
        data[field] = value
    data["version"] = current.version + 1
    data["updated_at"] = utc_now_iso()
    data["updated_by"] = actor[:120]
    return StoredRuntimeConfig.model_validate(data)


def effective_origin_url(settings: Settings, runtime: StoredRuntimeConfig) -> str:
    """Runtime value wins when it was set; an explicit empty string disables proxying."""
    if runtime.origin_url is not None and "origin_url" in runtime.model_fields_set:
        return runtime.origin_url.strip()
    return settings.origin_url.strip()


def origin_is_allowlisted(origin_url: str, allowlist: Optional[set[str]]) -> bool:
    """True when the URL's host is an allowlisted host (or a subdomain of one)."""
    host = (urlsplit(origin_url or "").hostname or "").lower()
    if not host:
        return False
    for entry in allowlist or set():
        entry = entry.strip().lower().lstrip(".")
        if entry and (host == entry or host.endswith("." + entry)):
            return True
    return False


def validate_origin_url(
    value: str,
    *,
    allow_private: bool = True,
    allowlist: Optional[set[str]] = None,
    require_allowlist: bool = False,
) -> str:
    """Validate an owner-supplied origin URL and, for public demos, block SSRF targets."""
    cleaned = (value or "").strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Origin URL must be an absolute http:// or https:// URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Origin URL must not contain credentials, a query, or a fragment.")
    host = parsed.hostname
    if not host:
        raise ValueError("Origin URL must include a host.")
    if allow_private:
        return cleaned
    if require_allowlist:
        raise OriginNotAllowedError(
            "Public demo configuration can only change the origin to a host listed in "
            "DEMO_ORIGIN_ALLOWLIST. Set DEMO_ORIGIN_ALLOWLIST or provide the admin "
            "token (X-Admin-Token)."
        )
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError(f"Origin host could not be resolved: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                "Public demo configuration cannot point at a private, loopback, or reserved address."
            )
    return cleaned


def validate_paths(paths: list[str], *, field: str, allow_root: bool = True) -> list[str]:
    cleaned: list[str] = []
    for raw in paths:
        path = str(raw).strip()
        if not path:
            continue
        if not path.startswith("/"):
            raise ValueError(f"{field} paths must start with '/': {path!r}")
        if "\\" in path or "\x00" in path:
            raise ValueError(f"{field} path contains an invalid character: {path!r}")
        parts = path.split("/")
        if any(part in (".", "..") for part in parts):
            raise ValueError(f"{field} path must be normalized (no '.' or '..'): {path!r}")
        if path != "/":
            path = "/" + "/".join(part for part in parts if part)
        if path == "/" and not allow_root:
            raise ValueError(f"{field} cannot be the root path '/'.")
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def validate_price(value: str, *, field: str) -> str:
    try:
        amount = Decimal(str(value).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a decimal amount.") from exc
    if amount <= 0:
        raise ValueError(f"{field} must be greater than 0.00.")
    if amount > Decimal("10000.00"):
        raise ValueError(f"{field} is unrealistically large.")
    return f"{amount:.2f}"


def bump_policy_version(current: str) -> str:
    """Increment the last numeric component: 1.0 -> 1.1, 2 -> 3, 1.2.3 -> 1.2.4."""
    parts = current.split(".")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].isdigit():
            parts[index] = str(int(parts[index]) + 1)
            return ".".join(parts)
    return f"{current}.1"


def origin_status(proxy: Optional[OriginProxy]) -> OriginStatus:
    if proxy is None:
        return OriginStatus(reachable=False, robots_imported=False)
    return OriginStatus(
        checked_at=proxy.last_checked_at,
        reachable=proxy.last_error is None,
        robots_imported=proxy.robots_imported,
        crawl_delay_seconds=proxy.rules.crawl_delay_seconds,
        disallowed_paths=list(proxy.rules.disallow),
        allowed_paths=list(proxy.rules.allow),
        error=proxy.last_error,
    )


async def check_origin(origin_url: str, timeout_seconds: float = 10.0) -> OriginStatus:
    """Fetch the origin's robots.txt without changing the running gateway."""
    proxy = OriginProxy(origin_url, timeout_seconds)
    await asyncio.to_thread(proxy.import_robots)
    return origin_status(proxy)
