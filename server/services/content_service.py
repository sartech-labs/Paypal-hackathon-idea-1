"""Local JSON-backed website content and policy configuration service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.config import Settings
from server.schemas import PolicyDecision, SiteConfig


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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._site_config = self._load_site_config(settings)
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

    def _load_site_config(self, settings: Settings) -> SiteConfig:
        raw = self._load_json_file(settings.site_config_path)
        # Allow environment overrides for key runtime limits and deposit amount
        raw["deposit_amount"] = f"{settings.deposit_amount:.2f}"
        raw["currency"] = settings.paypal_currency
        raw["max_requests_per_session"] = settings.max_requests_per_session
        raw["rate_limit_requests"] = settings.rate_limit_requests
        raw["rate_limit_window_seconds"] = settings.rate_limit_window_seconds
        raw["session_ttl_seconds"] = settings.session_ttl_seconds
        raw["challenge_ttl_seconds"] = settings.challenge_ttl_seconds
        raw["paypal_sandbox_enabled"] = settings.payment_mode == "paypal_sandbox"
        raw["pricing_tiers"] = self._apply_tier_price_overrides(raw.get("pricing_tiers") or [], settings)
        return SiteConfig.model_validate(raw)

    @staticmethod
    def _apply_tier_price_overrides(tiers: list[Any], settings: Settings) -> list[Any]:
        """Apply TIER1_PRICE..TIER3_PRICE: tier1 is the highest-ranked tier.

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

    def allowed_pages(self) -> list[PageContent]:
        """The allowed pages in file order (never includes disallowed pages)."""
        return list(self._allowed_pages.values())

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

