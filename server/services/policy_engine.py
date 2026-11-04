"""Policy evaluation engine for CrawlerTrust."""

from __future__ import annotations

from typing import Optional

from server.core.security import (
    is_timestamp_expired,
    normalize_and_validate_gateway_path,
    sanitize_path_for_display,
)
from server.schemas import (
    DepositRecord,
    DepositState,
    PolicyDecision,
    PolicyDecisionStatus,
    SessionRecord,
    SessionStatus,
    SiteConfig,
)
from server.services.rate_limiter import SlidingWindowRateLimiter


class PolicyEngine:
    """
    Pure, payment-provider-independent policy engine that evaluates crawler gateway
    requests and produces structured PolicyDecision results.
    """

    def __init__(
        self,
        site_config: SiteConfig,
        rate_limiter: SlidingWindowRateLimiter,
        known_allowed_pages: set[str] | None = None,
    ) -> None:
        self._site_config = site_config
        self._rate_limiter = rate_limiter
        self._known_allowed_pages = (
            set(known_allowed_pages)
            if known_allowed_pages is not None
            else set(site_config.allowed_paths)
        )

    @property
    def site_config(self) -> SiteConfig:
        return self._site_config

    @staticmethod
    def _rule_specificity(path: str, rule: str) -> int:
        """
        Return specificity score (> 0) if `rule` matches `path`, or -1 if no match.
        - Exact match on "/" has specificity 1 (does not prefix-match arbitrary paths).
        - Non-root rule "/foo" matches exact "/foo" or prefix "/foo/..." with specificity len(rule).
        """
        cleaned_rule = rule.strip()
        if not cleaned_rule:
            return -1
        if cleaned_rule == "/":
            return 1 if path == "/" else -1

        normalized_rule = "/" + "/".join(
            seg for seg in cleaned_rule.split("/") if seg
        )
        if path == normalized_rule:
            return len(normalized_rule) + 1
        if path.startswith(normalized_rule + "/"):
            return len(normalized_rule)
        return -1

    def classify_path(self, normalized_path: str) -> str:
        """
        Classify a normalized path into 'DISALLOWED', 'ALLOWED', or 'UNKNOWN'
        using longest-match specificity rules so more-specific rules are never
        accidentally overridden by broader rules.
        """
        best_disallowed = max(
            (
                self._rule_specificity(normalized_path, rule)
                for rule in self._site_config.disallowed_paths
            ),
            default=-1,
        )
        best_allowed_rule = max(
            (
                self._rule_specificity(normalized_path, rule)
                for rule in self._site_config.allowed_paths
            ),
            default=-1,
        )

        # If a disallowed rule matches and is at least as specific as any allowed rule, block it
        if best_disallowed >= 0 and best_disallowed >= best_allowed_rule:
            return "DISALLOWED"

        # To be served as ALLOWED, the path must match an allowed rule and exist in known allowed pages
        if best_allowed_rule >= 0 and normalized_path in self._known_allowed_pages:
            return "ALLOWED"

        return "UNKNOWN"

    def evaluate_request(
        self,
        *,
        path_param: str,
        raw_request_path: Optional[str] = None,
        session: Optional[SessionRecord],
        deposit: Optional[DepositRecord],
    ) -> PolicyDecision:
        """Evaluate all session, deposit, path, budget, and rate-limit policies."""
        max_budget = self._site_config.max_requests_per_session
        max_window = self._site_config.rate_limit_requests
        window_sec = self._site_config.rate_limit_window_seconds
        policy_ver = self._site_config.policy_version

        # 1. Missing or invalid session
        if session is None:
            return PolicyDecision(
                allowed=False,
                http_status=401,
                reason_code="INVALID_SESSION",
                reason="Missing or invalid crawler bearer token.",
                violation=False,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path="/",
                remaining_session_budget=0,
                remaining_window_requests=0,
            )

        remaining_budget = max(0, max_budget - session.allowed_requests)
        remaining_window, retry_after = self._rate_limiter.peek(
            session.session_id, max_window, window_sec
        )

        # 2. Revoked session
        if session.status == SessionStatus.REVOKED:
            return PolicyDecision(
                allowed=False,
                http_status=401,
                reason_code="SESSION_REVOKED",
                reason="Crawler session has been revoked.",
                violation=False,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path="/",
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
            )

        # 3. Expired session
        if session.status == SessionStatus.EXPIRED or is_timestamp_expired(
            session.expires_at
        ):
            return PolicyDecision(
                allowed=False,
                http_status=401,
                reason_code="SESSION_EXPIRED",
                reason="Crawler session has expired.",
                violation=False,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path="/",
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
            )

        # 4. Completed session reuse attempt
        if session.status == SessionStatus.COMPLETED:
            return PolicyDecision(
                allowed=False,
                http_status=403,
                reason_code="SESSION_ALREADY_COMPLETED",
                reason="Session has already been completed and can no longer access content.",
                violation=True,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path="/",
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
            )

        # 5. Deposit validation
        if (
            deposit is None
            or not deposit.is_funded
            or deposit.deposit_state not in (DepositState.CAPTURED, DepositState.AUTHORIZED)
            or deposit.policy_decision == PolicyDecisionStatus.FORFEITED_BY_POLICY
            or not deposit.policy_accepted
        ):
            return PolicyDecision(
                allowed=False,
                http_status=402,
                reason_code="DEPOSIT_NOT_ACTIVE",
                reason="Session does not have an active, verified captured deposit.",
                violation=False,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path="/",
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
            )

        # 6. Policy version check
        if session.policy_version != policy_ver:
            return PolicyDecision(
                allowed=False,
                http_status=403,
                reason_code="POLICY_VERSION_MISMATCH",
                reason=(
                    f"Session accepted policy version '{session.policy_version}', "
                    f"but site requires '{policy_ver}'."
                ),
                violation=False,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path="/",
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
            )

        # 7. Path normalization & traversal prevention
        normalized_path, is_valid_path, path_error = normalize_and_validate_gateway_path(
            path_param, raw_request_path
        )
        if not is_valid_path:
            return PolicyDecision(
                allowed=False,
                http_status=403,
                reason_code="INVALID_PATH",
                reason=f"Malformed or prohibited path traversal attempt: {path_error}",
                violation=True,
                disqualifying=self._site_config.refund_policy.disqualify_on_disallowed_path,
                policy_version=policy_ver,
                normalized_path=normalized_path,
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
                requested_path=sanitize_path_for_display(
                    raw_request_path if raw_request_path is not None else path_param
                ),
            )

        # 8. Disallowed path check (evaluated before serving or reading any content)
        path_class = self.classify_path(normalized_path)
        if path_class == "DISALLOWED":
            return PolicyDecision(
                allowed=False,
                http_status=403,
                reason_code="DISALLOWED_PATH",
                reason=f"Access to path '{normalized_path}' is prohibited by crawling policy.",
                violation=True,
                disqualifying=self._site_config.refund_policy.disqualify_on_disallowed_path,
                policy_version=policy_ver,
                normalized_path=normalized_path,
                remaining_session_budget=remaining_budget,
                remaining_window_requests=remaining_window,
            )

        # 9. Sliding-window rate limit check (applies even to unknown paths to prevent scanning floods)
        window_ok, rem_window_after, retry_sec = self._rate_limiter.check_and_consume(
            session.session_id, max_window, window_sec
        )
        if not window_ok:
            will_disqualify = (
                session.rate_limit_violations + 1
                >= self._site_config.refund_policy.max_rate_limit_violations_before_disqualification
            )
            return PolicyDecision(
                allowed=False,
                http_status=429,
                reason_code="RATE_LIMIT_EXCEEDED",
                reason=(
                    f"Rate limit of {max_window} requests per {window_sec:g}s window exceeded."
                ),
                violation=True,
                disqualifying=will_disqualify,
                policy_version=policy_ver,
                normalized_path=normalized_path,
                remaining_session_budget=remaining_budget,
                remaining_window_requests=0,
                retry_after_seconds=retry_sec,
            )

        # 10. Session maximum request budget check
        if session.allowed_requests >= max_budget:
            will_disqualify = (
                session.budget_violations + 1
                >= self._site_config.refund_policy.max_budget_violations_before_disqualification
            )
            return PolicyDecision(
                allowed=False,
                http_status=429,
                reason_code="MAX_REQUESTS_EXCEEDED",
                reason=(
                    f"Maximum session request budget ({max_budget} allowed requests) exceeded."
                ),
                violation=True,
                disqualifying=will_disqualify,
                policy_version=policy_ver,
                normalized_path=normalized_path,
                remaining_session_budget=0,
                remaining_window_requests=rem_window_after,
            )

        # 11. Unknown path check (returns 404 without disqualifying violation)
        if path_class == "UNKNOWN":
            return PolicyDecision(
                allowed=False,
                http_status=404,
                reason_code="UNKNOWN_PATH",
                reason=f"Requested path '{normalized_path}' does not exist.",
                violation=False,
                disqualifying=False,
                policy_version=policy_ver,
                normalized_path=normalized_path,
                remaining_session_budget=remaining_budget,
                remaining_window_requests=rem_window_after,
            )

        # 12. Authorized access
        return PolicyDecision(
            allowed=True,
            http_status=200,
            reason_code="ALLOWED",
            reason="Request authorized by crawling policy.",
            violation=False,
            disqualifying=False,
            policy_version=policy_ver,
            normalized_path=normalized_path,
            remaining_session_budget=max(0, remaining_budget - 1),
            remaining_window_requests=rem_window_after,
        )

