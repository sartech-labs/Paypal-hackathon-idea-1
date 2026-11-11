"""Multi-site registry persisted in the gateway configuration file.

The registry replaces the old flat ``StoredRuntimeConfig`` file. Legacy flat files are
migrated in memory into a single site, and nothing is written until the first mutation.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError

from server.core.security import PersistenceCorruptionError, utc_now_iso
from server.services.runtime_config import StoredRuntimeConfig


class GatewaySite(BaseModel):
    """One configured website and its owner-editable runtime config."""

    site_id: str
    name: str
    status: Literal["live", "paused"] = "live"
    config: StoredRuntimeConfig = Field(default_factory=StoredRuntimeConfig)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class SiteRegistryData(BaseModel):
    active_site_id: Optional[str] = None
    sites: list[GatewaySite] = Field(default_factory=list)


def host_of(origin_url: Optional[str]) -> str:
    """The lower-case host of an origin URL, or an empty string."""
    if not origin_url:
        return ""
    parsed = urlsplit(str(origin_url).strip())
    host = parsed.hostname
    if not host and "://" not in str(origin_url):
        host = urlsplit("//" + str(origin_url).strip()).hostname
    return (host or "").lower()


def slugify_site_id(origin_url: Optional[str]) -> str:
    """Build a stable, URL-safe site id from an origin host (www.example.com)."""
    host = host_of(origin_url)
    if not host:
        return "default"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    return slug or "default"


class SiteRegistryStore:
    """Atomic JSON storage for the multi-site registry."""

    def __init__(
        self,
        path: Path,
        *,
        default_origin_url: str = "",
        default_name: Optional[str] = None,
    ) -> None:
        self._path = path
        self._default_origin_url = (default_origin_url or "").strip()
        self._default_name = (default_name or "").strip()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SiteRegistryData:
        """Load the registry, migrating a legacy flat runtime config into one site."""
        if not self._path.exists():
            return self.default_registry()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected a JSON object")
            if "sites" not in raw:
                return self._migrate_legacy(raw)
            data = SiteRegistryData.model_validate(raw)
            if data.active_site_id not in {site.site_id for site in data.sites}:
                data.active_site_id = data.sites[0].site_id if data.sites else None
            return data
        except (OSError, ValueError, ValidationError) as exc:
            raise PersistenceCorruptionError(
                f"Malformed site registry at {self._path}: {exc}"
            ) from exc

    def save(self, data: SiteRegistryData) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_site_id": data.active_site_id,
            "sites": [
                {
                    "site_id": site.site_id,
                    "name": site.name,
                    "status": site.status,
                    "created_at": site.created_at,
                    "updated_at": site.updated_at,
                    "config": self._config_payload(site.config),
                }
                for site in data.sites
            ],
        }
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self._path)

    @staticmethod
    def _config_payload(config: StoredRuntimeConfig) -> dict[str, Any]:
        payload = config.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        payload.setdefault("version", config.version)
        if config.updated_at is not None:
            payload.setdefault("updated_at", config.updated_at)
        if config.updated_by is not None:
            payload.setdefault("updated_by", config.updated_by)
        return payload

    def default_registry(self) -> SiteRegistryData:
        """The in-memory site used when the registry file does not exist yet."""
        return self._build_default_registry()

    def _build_default_registry(self) -> SiteRegistryData:
        origin = self._default_origin_url
        site_id = slugify_site_id(origin)
        name = self._default_name or host_of(origin) or "SARA demo site"
        now = utc_now_iso()
        # Inherit ORIGIN_URL from the environment instead of persisting it as an override.
        config = StoredRuntimeConfig()
        site = GatewaySite(
            site_id=site_id,
            name=name,
            status="live",
            config=config,
            created_at=now,
            updated_at=now,
        )
        return SiteRegistryData(active_site_id=site_id, sites=[site])

    def _migrate_legacy(self, raw: dict[str, Any]) -> SiteRegistryData:
        config = StoredRuntimeConfig.model_validate(raw)
        origin = config.origin_url or self._default_origin_url
        site_id = slugify_site_id(origin)
        name = host_of(origin) or "SARA demo site"
        now = utc_now_iso()
        site = GatewaySite(
            site_id=site_id,
            name=name,
            status="live",
            config=config,
            created_at=config.updated_at or now,
            updated_at=config.updated_at or now,
        )
        return SiteRegistryData(active_site_id=site_id, sites=[site])
