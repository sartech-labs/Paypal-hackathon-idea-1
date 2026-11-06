"""Tier-based content pricing: which deposit a protected resource requires.

The tiers live in one place, `pricing_tiers` in data/site_config.json, and are served to
crawlers at /policies. The gateway resolves the tier from the requested path. It never
takes a price from the crawler or the browser.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from server.core.security import normalize_and_validate_gateway_path
from server.schemas import PricingTier, SiteConfig


class PricingConfigError(ValueError):
    """The pricing tier configuration is inconsistent."""


def _normalize_prefix(prefix: str) -> str:
    return "/" + "/".join(segment for segment in prefix.strip().split("/") if segment)


class PricingCatalog:
    """Resolves resource paths to pricing tiers and decides whether a deposit covers a path."""

    def __init__(self, site_config: SiteConfig) -> None:
        self._currency = site_config.currency
        self._base_amount = site_config.deposit_amount
        tiers: list[PricingTier] = []
        seen_ids: set[str] = set()
        seen_prefixes: set[str] = set()
        for tier in site_config.pricing_tiers:
            if tier.tier_id in seen_ids:
                raise PricingConfigError(f"Duplicate pricing tier id: {tier.tier_id}")
            prefix = _normalize_prefix(tier.path_prefix)
            extra = [_normalize_prefix(p) for p in tier.additional_path_prefixes]
            for candidate in [prefix, *extra]:
                if candidate == "/" or candidate in seen_prefixes:
                    raise PricingConfigError(f"Pricing tier {tier.tier_id} needs unique, non-root path prefixes")
                seen_prefixes.add(candidate)
            try:
                price = Decimal(tier.price).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError) as exc:
                raise PricingConfigError(f"Invalid price for tier {tier.tier_id}: {tier.price!r}") from exc
            if price <= 0:
                raise PricingConfigError(f"Price for tier {tier.tier_id} must be positive")
            if tier.currency.upper() != self._currency:
                raise PricingConfigError(
                    f"Tier {tier.tier_id} is priced in {tier.currency}, but deposits are in {self._currency}"
                )
            for resource in tier.resources:
                if not (resource.path == prefix or resource.path.startswith(prefix + "/")):
                    raise PricingConfigError(
                        f"Resource {resource.path} is outside the {tier.tier_id} tier prefix {prefix}"
                    )
            seen_ids.add(tier.tier_id)
            tiers.append(
                tier.model_copy(
                    update={
                        "path_prefix": prefix,
                        "additional_path_prefixes": extra,
                        "price": f"{price:.2f}",
                        "currency": self._currency,
                    }
                )
            )
        # Longest prefix first so nested prefixes resolve to the most specific tier.
        self._prefixes = sorted(
            ((p, t) for t in tiers for p in [t.path_prefix, *t.additional_path_prefixes]),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._by_id = {tier.tier_id: tier for tier in tiers}
        self._ordered = sorted(tiers, key=lambda t: t.rank, reverse=True)

    @property
    def tiers(self) -> list[PricingTier]:
        """Tiers from most to least valuable."""
        return list(self._ordered)

    def max_price(self, floor: Decimal = Decimal("5.00")) -> Decimal:
        """Highest deposit this gateway can ask for (at least `floor`)."""
        prices = [Decimal(tier.price) for tier in self._ordered] + [Decimal(self._base_amount)]
        return max([floor, *prices])

    def get(self, tier_id: Optional[str]) -> Optional[PricingTier]:
        return self._by_id.get(tier_id) if tier_id else None

    def validate_resources(self, known_pages: Iterable[str]) -> None:
        """Every resource a tier sells must be a real, servable page."""
        known = set(known_pages)
        for tier in self._ordered:
            for resource in tier.resources:
                if resource.path not in known:
                    raise PricingConfigError(f"Tier {tier.tier_id} lists {resource.path}, which is not an allowed page")

    def tier_for_normalized_path(self, normalized_path: str) -> Optional[PricingTier]:
        for prefix, tier in self._prefixes:
            if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
                return tier
        return None

    def tier_for_request_path(self, raw_path: str) -> Optional[PricingTier]:
        """Tier for a raw gateway path; malformed paths get no tier (the base deposit)."""
        normalized, valid, _error = normalize_and_validate_gateway_path(raw_path.lstrip("/"), raw_path)
        if not valid:
            return None
        return self.tier_for_normalized_path(normalized)

    def deposit_for_request_path(self, raw_path: str) -> tuple[str, Optional[PricingTier]]:
        """Deposit amount (2-decimal string) the gateway requires for a requested path."""
        tier = self.tier_for_request_path(raw_path)
        return (tier.price if tier else self._base_amount), tier

    @staticmethod
    def rank_of(tier: Optional[PricingTier]) -> int:
        return tier.rank if tier else 0

    def deposit_covers(self, deposit_tier_id: Optional[str], normalized_path: str) -> tuple[bool, Optional[PricingTier]]:
        """Whether a deposit paid at `deposit_tier_id` covers the tier of `normalized_path`."""
        required = self.tier_for_normalized_path(normalized_path)
        if required is None:
            return True, None
        paid = self.get(deposit_tier_id)
        return self.rank_of(paid) >= required.rank, required
