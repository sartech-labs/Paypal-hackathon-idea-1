"""Crawler identity and HTTP 402 challenge management."""

from __future__ import annotations

import secrets
import uuid
from typing import Optional

from server.core.security import (
    is_timestamp_expired,
    utc_future_iso,
    utc_now_iso,
    validate_crawler_id,
)
from server.schemas import (
    ChallengeDepositInfo,
    ChallengePricingInfo,
    ChallengeRecord,
    ChallengeResponse,
    RulesSummary,
    SiteConfig,
)
from server.services.pricing import PricingCatalog


class IdentityService:
    """Generates and validates crawler identities and single-use 402 challenges."""

    def __init__(self, site_config: SiteConfig, pricing: Optional[PricingCatalog] = None) -> None:
        self._site_config = site_config
        self._pricing = pricing or PricingCatalog(site_config)

    def resolve_or_generate_crawler_id(self, candidate: Optional[str]) -> str:
        """Validate a provided crawler_id or generate a fresh unique crawler_id."""
        if candidate and candidate.strip():
            return validate_crawler_id(candidate)
        return f"crawler-{secrets.token_hex(4)}"

    def build_challenge(
        self,
        crawler_id: str,
        requested_path: str,
        challenge_ttl_seconds: int,
    ) -> tuple[ChallengeRecord, ChallengeResponse]:
        """Create a fresh, unpredictable HTTP 402 challenge bound to crawler_id."""
        valid_crawler_id = validate_crawler_id(crawler_id)
        challenge_id = f"chg_{uuid.uuid4().hex}"
        now_iso = utc_now_iso()
        expires_iso = utc_future_iso(challenge_ttl_seconds)
        # The deposit is priced by the gateway from the requested resource's tier.
        amount, tier = self._pricing.deposit_for_request_path(requested_path)

        record = ChallengeRecord(
            challenge_id=challenge_id,
            crawler_id=valid_crawler_id,
            site_id=self._site_config.site_id,
            policy_version=self._site_config.policy_version,
            required_amount=amount,
            currency=self._site_config.currency,
            requested_path=requested_path,
            created_at=now_iso,
            expires_at=expires_iso,
            consumed=False,
            deposit_id=None,
            pricing_tier=tier.tier_id if tier else None,
            pricing_tier_name=tier.name if tier else None,
        )

        response = ChallengeResponse(
            challenge_id=challenge_id,
            crawler_id=valid_crawler_id,
            site_id=self._site_config.site_id,
            policy_version=self._site_config.policy_version,
            deposit=ChallengeDepositInfo(
                amount=amount,
                currency=self._site_config.currency,
                refundable=self._site_config.refundable,
            ),
            pricing=(
                ChallengePricingInfo(
                    tier_id=tier.tier_id,
                    tier_name=tier.name,
                    price=tier.price,
                    currency=tier.currency,
                    resource=requested_path,
                )
                if tier
                else None
            ),
            rules_summary=RulesSummary(
                allowed_paths=list(self._site_config.allowed_paths),
                disallowed_paths=list(self._site_config.disallowed_paths),
                max_requests_per_session=self._site_config.max_requests_per_session,
                rate_limit_requests=self._site_config.rate_limit_requests,
                rate_limit_window_seconds=self._site_config.rate_limit_window_seconds,
                session_ttl_seconds=self._site_config.session_ttl_seconds,
            ),
            expires_at=expires_iso,
        )
        return record, response

    @staticmethod
    def validate_challenge_for_deposit(
        challenge: ChallengeRecord,
        crawler_id: str,
    ) -> None:
        """
        Verify that a challenge is owned by crawler_id, not expired, and not already bound
        to a different deposit.
        """
        if challenge.crawler_id != crawler_id:
            raise PermissionError(
                "Challenge belongs to a different crawler_id and cannot be claimed"
            )
        if is_timestamp_expired(challenge.expires_at):
            raise ValueError("Challenge has expired; request a new 402 challenge from /gateway")

