"""Unit tests for the PolicyEngine independent of PayPal and FastAPI."""

from __future__ import annotations

from server.core.security import utc_future_iso, utc_now_iso
from server.schemas import (
    DepositRecord,
    DepositState,
    PaymentMode,
    PaymentStatus,
    PolicyDecisionStatus,
    RefundPolicyConfig,
    RefundTransactionStatus,
    SessionRecord,
    SessionStatus,
    SiteConfig,
)
from server.services.policy_engine import PolicyEngine
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock


def _make_site_config() -> SiteConfig:
    return SiteConfig(
        site_id="test-site",
        site_name="Test News",
        policy_version="1.0",
        deposit_amount="1.00",
        currency="USD",
        refundable=True,
        max_requests_per_session=5,
        rate_limit_requests=2,
        rate_limit_window_seconds=10.0,
        session_ttl_seconds=1800,
        challenge_ttl_seconds=600,
        allowed_paths=["/", "/news", "/news/technology", "/admin/public"],
        disallowed_paths=["/admin", "/news/technology/secret"],
        refund_policy=RefundPolicyConfig(
            description="Test refund policy",
            disqualify_on_disallowed_path=True,
            max_rate_limit_violations_before_disqualification=2,
            max_budget_violations_before_disqualification=1,
            require_explicit_session_completion=True,
        ),
    )


def _make_active_session_and_deposit() -> tuple[SessionRecord, DepositRecord]:
    now = utc_now_iso()
    exp = utc_future_iso(1800)
    session = SessionRecord(
        session_id="ses_unit_1",
        crawler_id="crawler-unit",
        challenge_id="chg_unit_1",
        deposit_id="dep_unit_1",
        policy_version="1.0",
        policy_accepted_at=now,
        payment_reference="mock_cap_1",
        payment_mode=PaymentMode.MOCK,
        token_hash="a" * 64,
        status=SessionStatus.ACTIVE,
        created_at=now,
        expires_at=exp,
    )
    deposit = DepositRecord(
        deposit_id="dep_unit_1",
        challenge_id="chg_unit_1",
        crawler_id="crawler-unit",
        site_id="test-site",
        policy_version="1.0",
        required_amount="1.00",
        captured_amount="1.00",
        currency="USD",
        payment_mode=PaymentMode.MOCK,
        deposit_state=DepositState.CAPTURED,
        payment_status=PaymentStatus.CAPTURED,
        policy_decision=PolicyDecisionStatus.UNDECIDED,
        refund_status=RefundTransactionStatus.NOT_REQUESTED,
        policy_accepted=True,
        policy_accepted_at=now,
        session_id="ses_unit_1",
        provider_order_id="mock_ord_1",
        provider_capture_id="mock_cap_1",
        order_request_id="req_1",
        capture_request_id="req_2",
        refund_request_id="req_3",
        created_at=now,
        updated_at=now,
        captured_at=now,
    )
    return session, deposit


def test_policy_specificity_precedence() -> None:
    config = _make_site_config()
    limiter = SlidingWindowRateLimiter(clock=FakeClock())
    engine = PolicyEngine(
        site_config=config,
        rate_limiter=limiter,
        known_allowed_pages={"/", "/news", "/news/technology", "/admin/public"},
    )
    session, deposit = _make_active_session_and_deposit()

    # /admin is disallowed (403)
    d1 = engine.evaluate_request(path_param="/admin", session=session, deposit=deposit)
    assert not d1.allowed and d1.http_status == 403 and d1.reason_code == "DISALLOWED_PATH"

    # /admin/public is more specific than /admin, so it is allowed (200)
    d2 = engine.evaluate_request(
        path_param="/admin/public", session=session, deposit=deposit
    )
    assert d2.allowed and d2.http_status == 200

    # /news/technology/secret is more specific than /news/technology, so it is disallowed (403)
    d3 = engine.evaluate_request(
        path_param="/news/technology/secret", session=session, deposit=deposit
    )
    assert not d3.allowed and d3.http_status == 403 and d3.reason_code == "DISALLOWED_PATH"


def test_policy_engine_checks_budget_and_version_mismatch() -> None:
    config = _make_site_config()
    limiter = SlidingWindowRateLimiter(clock=FakeClock())
    engine = PolicyEngine(
        site_config=config,
        rate_limiter=limiter,
        known_allowed_pages={"/", "/news"},
    )
    session, deposit = _make_active_session_and_deposit()

    # Simulate budget exhausted
    session.allowed_requests = config.max_requests_per_session
    decision_budget = engine.evaluate_request(
        path_param="/news", session=session, deposit=deposit
    )
    assert not decision_budget.allowed
    assert decision_budget.http_status == 429
    assert decision_budget.reason_code == "MAX_REQUESTS_EXCEEDED"
    assert decision_budget.violation is True
    assert decision_budget.disqualifying is True

    # Simulate policy version mismatch
    session.allowed_requests = 0
    session.policy_version = "0.9"
    decision_ver = engine.evaluate_request(
        path_param="/news", session=session, deposit=deposit
    )
    assert not decision_ver.allowed
    assert decision_ver.http_status == 403
    assert decision_ver.reason_code == "POLICY_VERSION_MISMATCH"

