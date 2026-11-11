"""Local JSON-backed website content and policy configuration service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from server.config import Settings
from server.schemas import PolicyDecision, SiteConfig
from server.services.runtime_config import StoredRuntimeConfig, validate_price


@dataclass(frozen=True)
class PageContent:
    path: str
    title: str
    content_type: str
    category: str
    body: str


class ContentService:
    """
    Loads website policy configuration, robots.txt metadata, and allowed pages.
    Never exposes or returns disallowed page content through the gateway.
    """

    def __init__(
        self,
        settings: Settings,
        runtime: Optional[StoredRuntimeConfig] = None,
        *,
        site_id: Optional[str] = None,
        site_name: Optional[str] = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime or StoredRuntimeConfig()
        self._site_id = site_id
        self._site_name = site_name
        self._raw_site_config = self._load_json_file(settings.site_config_path)
        self._site_config = self.build_site_config(self._runtime)
        self._robots_data = self._load_json_file(settings.robots_config_path)
        self._allowed_pages = self._load_allowed_pages(settings.allowed_pages_path)
        # Validate disallowed_pages.json structure at startup without storing its bodies
        # in the gateway serving dictionary so it can never be accidentally served.
        self._disallowed_paths_metadata = self._validate_disallowed_pages(
            settings.disallowed_pages_path
        )

    @staticmethod
    def _load_json_file(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Required configuration data file not found: {path}")
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return data

    def build_site_config(self, runtime: Optional[StoredRuntimeConfig] = None) -> SiteConfig:
        """Build the effective SiteConfig: data JSON -> env defaults -> stored runtime overlay."""
        runtime = runtime or self._runtime
        settings = self._settings
        raw = json.loads(json.dumps(self._raw_site_config))
        if self._site_id is not None:
            raw["site_id"] = self._site_id
        if self._site_name is not None:
            raw["site_name"] = self._site_name
        # Deployment defaults from the environment.
        raw["deposit_amount"] = f"{settings.deposit_amount:.2f}"
        raw["currency"] = settings.paypal_currency
        raw["max_requests_per_session"] = settings.max_requests_per_session
        raw["rate_limit_requests"] = settings.rate_limit_requests
        raw["rate_limit_window_seconds"] = settings.rate_limit_window_seconds
        raw["session_ttl_seconds"] = settings.session_ttl_seconds
        raw["challenge_ttl_seconds"] = settings.challenge_ttl_seconds
        raw["paypal_sandbox_enabled"] = settings.payment_mode == "paypal_sandbox"
        # Owner-console overrides, applied live.
        if runtime.deposit_amount is not None:
            raw["deposit_amount"] = validate_price(runtime.deposit_amount, field="deposit_amount")
        if runtime.allowed_paths is not None:
            raw["allowed_paths"] = list(runtime.allowed_paths)
        if runtime.disallowed_paths is not None:
            raw["disallowed_paths"] = list(runtime.disallowed_paths)
        if runtime.max_requests_per_session is not None:
            raw["max_requests_per_session"] = runtime.max_requests_per_session
        if runtime.rate_limit_requests is not None:
            raw["rate_limit_requests"] = runtime.rate_limit_requests
        if runtime.rate_limit_window_seconds is not None:
            raw["rate_limit_window_seconds"] = runtime.rate_limit_window_seconds
        if runtime.policy_version is not None:
            raw["policy_version"] = runtime.policy_version
        if runtime.pricing_tiers is not None:
            # An explicit list (including []) replaces the bundled demo tiers.
            raw["pricing_tiers"] = [
                tier.model_dump(mode="json") for tier in runtime.pricing_tiers
            ]
        raw["pricing_tiers"] = self._apply_tier_price_overrides(
            raw.get("pricing_tiers") or [], settings, runtime.tier_prices
        )
        return SiteConfig.model_validate(raw)

    def apply_runtime(self, runtime: StoredRuntimeConfig, site_config: Optional[SiteConfig] = None) -> None:
        """Swap the effective configuration after a successful live update."""
        self._runtime = runtime
        self._site_config = site_config or self.build_site_config(runtime)

    @staticmethod
    def _apply_tier_price_overrides(
        tiers: list[Any], settings: Settings, runtime_prices: Optional[dict[str, str]] = None
    ) -> list[Any]:
        """Apply TIER1_PRICE..TIER3_PRICE, then owner-console tier prices by tier_id.

        Tier prices are charged in the deposit currency (PAYPAL_CURRENCY).
        """
        ordered = sorted(
            (dict(tier) for tier in tiers if isinstance(tier, dict)),
            key=lambda tier: int(tier.get("rank", 0)),
            reverse=True,
        )
        for index, tier in enumerate(ordered):
            override = settings.tier_prices[index] if index < len(settings.tier_prices) else None
            if override is not None:
                tier["price"] = f"{override:.2f}"
            runtime_price = (runtime_prices or {}).get(str(tier.get("tier_id", "")))
            if runtime_price is not None:
                tier["price"] = validate_price(runtime_price, field=f"tier {tier.get('tier_id')} price")
            tier["currency"] = settings.paypal_currency
        return ordered

    def _load_allowed_pages(self, path: Path) -> dict[str, PageContent]:
        raw = self._load_json_file(path)
        pages: dict[str, PageContent] = {}
        for page_path, entry in raw.items():
            if not isinstance(entry, dict) or "body" not in entry:
                raise ValueError(f"Invalid allowed page entry for {page_path} in {path}")
            pages[page_path] = PageContent(
                path=page_path,
                title=str(entry.get("title", page_path)),
                content_type=str(entry.get("content_type", "text/html; charset=utf-8")),
                category=str(entry.get("category", "general")),
                body=str(entry["body"]),
            )
        return pages

    def _validate_disallowed_pages(self, path: Path) -> set[str]:
        raw = self._load_json_file(path)
        for page_path, entry in raw.items():
            if not isinstance(entry, dict) or "body" not in entry:
                raise ValueError(f"Invalid disallowed page entry for {page_path} in {path}")
        return set(raw.keys())

    @property
    def site_config(self) -> SiteConfig:
        return self._site_config

    @property
    def allowed_page_paths(self) -> set[str]:
        return set(self._allowed_pages.keys())

    def render_robots_txt(self) -> str:
        """Render the configured robots.json rules as standard plain-text robots.txt."""
        lines: list[str] = [
            f"# robots.txt for {self._site_config.site_name} ({self._site_config.site_id})",
            f"# Policy Version: {self._site_config.policy_version}",
            f"# Notice: {self._robots_data.get('notice', '')}",
            "",
        ]
        groups = self._robots_data.get("groups", [])
        for group in groups:
            ua = group.get("user_agent", "*")
            lines.append(f"User-agent: {ua}")
            for allow_path in group.get("allow", []):
                lines.append(f"Allow: {allow_path}")
            for disallow_path in group.get("disallow", []):
                lines.append(f"Disallow: {disallow_path}")
            if "crawl_delay" in group:
                lines.append(f"Crawl-delay: {group['crawl_delay']}")
            if "request_rate" in group:
                lines.append(f"# Request-rate: {group['request_rate']}")
            lines.append("")
        lines.append(f"# Policy-Endpoint: {self._robots_data.get('policy_endpoint', '/policies')}")
        return "\n".join(lines).strip() + "\n"

    def get_authorized_content(self, decision: PolicyDecision) -> PageContent:
        """
        Return page content ONLY when `decision.allowed` is True and the path is in
        the allowed pages registry. Raises PermissionError if called on an unauthorized decision.
        """
        if not decision.allowed or decision.reason_code != "ALLOWED":
            raise PermissionError(
                f"ContentService refused to serve content for unauthorized decision: {decision.reason_code}"
            )
        if decision.normalized_path in self._disallowed_paths_metadata:
            raise PermissionError(
                f"Path '{decision.normalized_path}' is marked disallowed and can never be served"
            )
        page = self._allowed_pages.get(decision.normalized_path)
        if page is None:
            raise KeyError(f"Allowed page not found: {decision.normalized_path}")
        return page

