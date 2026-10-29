"""Pydantic models, enums, and state transition definitions for CrawlerTrust."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PaymentMode(str, Enum):
    MOCK = "mock"
    PAYPAL_SANDBOX = "paypal_sandbox"


class PaymentStatus(str, Enum):
    """Status of the inbound deposit payment transaction."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"


class PolicyDecisionStatus(str, Enum):
    """Server-side policy accountability decision for the session/deposit."""

    UNDECIDED = "UNDECIDED"
    ELIGIBLE_FOR_REFUND = "ELIGIBLE_FOR_REFUND"
    FORFEITED_BY_POLICY = "FORFEITED_BY_POLICY"


class RefundTransactionStatus(str, Enum):
    """Status of the outbound refund financial operation."""

    NOT_REQUESTED = "NOT_REQUESTED"
    REFUND_IN_PROGRESS = "REFUND_IN_PROGRESS"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    REFUND_FAILED = "REFUND_FAILED"
    NOT_APPLICABLE_FORFEITED = "NOT_APPLICABLE_FORFEITED"


class DepositState(str, Enum):
    """High-level composite deposit lifecycle state."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CAPTURED = "CAPTURED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    FORFEITED = "FORFEITED"
    FAILED = "FAILED"


class SessionStatus(str, Enum):
    """Lifecycle status of an authenticated crawler session."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


# Explicit state machine transition maps
ALLOWED_PAYMENT_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    PaymentStatus.PENDING: {
        PaymentStatus.PENDING,
        PaymentStatus.APPROVED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
    },
    PaymentStatus.APPROVED: {
        PaymentStatus.APPROVED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
    },
    PaymentStatus.CAPTURED: {PaymentStatus.CAPTURED},
    PaymentStatus.FAILED: {PaymentStatus.PENDING, PaymentStatus.FAILED},
}

ALLOWED_POLICY_DECISION_TRANSITIONS: dict[PolicyDecisionStatus, set[PolicyDecisionStatus]] = {
    PolicyDecisionStatus.UNDECIDED: {
        PolicyDecisionStatus.UNDECIDED,
        PolicyDecisionStatus.ELIGIBLE_FOR_REFUND,
        PolicyDecisionStatus.FORFEITED_BY_POLICY,
    },
    PolicyDecisionStatus.ELIGIBLE_FOR_REFUND: {PolicyDecisionStatus.ELIGIBLE_FOR_REFUND},
    PolicyDecisionStatus.FORFEITED_BY_POLICY: {PolicyDecisionStatus.FORFEITED_BY_POLICY},
}

ALLOWED_REFUND_TRANSITIONS: dict[RefundTransactionStatus, set[RefundTransactionStatus]] = {
    RefundTransactionStatus.NOT_REQUESTED: {
        RefundTransactionStatus.REFUND_IN_PROGRESS,
        RefundTransactionStatus.NOT_APPLICABLE_FORFEITED,
    },
    RefundTransactionStatus.REFUND_IN_PROGRESS: {
        RefundTransactionStatus.REFUND_PENDING,
        RefundTransactionStatus.REFUNDED,
        RefundTransactionStatus.REFUND_FAILED,
    },
    RefundTransactionStatus.REFUND_PENDING: {
        RefundTransactionStatus.REFUND_IN_PROGRESS,
        RefundTransactionStatus.REFUND_PENDING,
        RefundTransactionStatus.REFUNDED,
        RefundTransactionStatus.REFUND_FAILED,
    },
    RefundTransactionStatus.REFUND_FAILED: {
        RefundTransactionStatus.REFUND_IN_PROGRESS,
        RefundTransactionStatus.REFUND_FAILED,
    },
    RefundTransactionStatus.REFUNDED: {RefundTransactionStatus.REFUNDED},
    RefundTransactionStatus.NOT_APPLICABLE_FORFEITED: {
        RefundTransactionStatus.NOT_APPLICABLE_FORFEITED
    },
}

ALLOWED_SESSION_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.ACTIVE: {
        SessionStatus.ACTIVE,
        SessionStatus.COMPLETED,
        SessionStatus.REVOKED,
        SessionStatus.EXPIRED,
    },
    SessionStatus.COMPLETED: {SessionStatus.COMPLETED},
    SessionStatus.REVOKED: {SessionStatus.REVOKED},
    SessionStatus.EXPIRED: {SessionStatus.EXPIRED},
}


class RefundPolicyConfig(BaseModel):
    description: str
    disqualify_on_disallowed_path: bool = True
    max_rate_limit_violations_before_disqualification: int = 2
    max_budget_violations_before_disqualification: int = 1
    require_explicit_session_completion: bool = True


class SiteConfig(BaseModel):
    site_id: str
    site_name: str
    policy_version: str
    deposit_amount: str
    currency: str = "USD"
    refundable: bool = True
    max_requests_per_session: int = 10
    rate_limit_requests: int = 3
    rate_limit_window_seconds: float = 10.0
    session_ttl_seconds: int = 1800
    challenge_ttl_seconds: int = 600
    paypal_sandbox_enabled: bool = False
    allowed_paths: list[str]
    disallowed_paths: list[str]
    refund_policy: RefundPolicyConfig


class ChallengeDepositInfo(BaseModel):
    amount: str
    currency: str
    refundable: bool


class RulesSummary(BaseModel):
    allowed_paths: list[str]
    disallowed_paths: list[str]
    max_requests_per_session: int
    rate_limit_requests: int
    rate_limit_window_seconds: float
    session_ttl_seconds: int


class ChallengeResponse(BaseModel):
    error: str = "payment_required"
    challenge_id: str
    crawler_id: str
    site_id: str
    policy_version: str
    deposit: ChallengeDepositInfo
    rules_summary: RulesSummary
    policy_url: str = "/policies"
    payment_url: str = "/deposits/create"
    session_url: str = "/sessions/accept-policy"
    expires_at: str
    notice: str = (
        "Payment alone does not grant unrestricted access. You must verify the deposit, "
        "explicitly accept the current crawling policy version, and comply with all path "
        "and rate-limit rules to remain eligible for a deposit refund."
    )
    message: str = "Complete the deposit and accept the crawling policy."


class ChallengeRecord(BaseModel):
    challenge_id: str
    crawler_id: str
    site_id: str
    policy_version: str
    required_amount: str
    currency: str
    requested_path: str
    created_at: str
    expires_at: str
    consumed: bool = False
    deposit_id: Optional[str] = None


class DepositCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(..., min_length=8, max_length=128)
    crawler_id: str = Field(..., min_length=2, max_length=128)


class DepositRecord(BaseModel):
    deposit_id: str
    challenge_id: str
    crawler_id: str
    site_id: str
    policy_version: str
    required_amount: str
    captured_amount: Optional[str] = None
    currency: str
    payment_mode: PaymentMode
    deposit_state: DepositState
    payment_status: PaymentStatus
    policy_decision: PolicyDecisionStatus = PolicyDecisionStatus.UNDECIDED
    refund_status: RefundTransactionStatus = RefundTransactionStatus.NOT_REQUESTED
    policy_accepted: bool = False
    policy_accepted_at: Optional[str] = None
    session_id: Optional[str] = None
    provider_order_id: Optional[str] = None
    provider_order_status: Optional[str] = None
    approval_url: Optional[str] = None
    provider_capture_id: Optional[str] = None
    provider_refund_id: Optional[str] = None
    provider_refund_status: Optional[str] = None
    order_request_id: str
    capture_request_id: str
    refund_request_id: str
    failure_reason: Optional[str] = None
    decision_reason: Optional[str] = None
    created_at: str
    updated_at: str
    captured_at: Optional[str] = None
    refunded_at: Optional[str] = None
    forfeited_at: Optional[str] = None


class DepositResponse(BaseModel):
    deposit_id: str
    challenge_id: str
    crawler_id: str
    site_id: str
    policy_version: str
    required_amount: str
    captured_amount: Optional[str] = None
    currency: str
    payment_mode: PaymentMode
    simulated: bool
    deposit_state: DepositState
    payment_status: PaymentStatus
    policy_decision: PolicyDecisionStatus
    refund_status: RefundTransactionStatus
    policy_accepted: bool
    policy_accepted_at: Optional[str] = None
    session_id: Optional[str] = None
    provider_order_id: Optional[str] = None
    provider_order_status: Optional[str] = None
    approval_url: Optional[str] = None
    provider_capture_id: Optional[str] = None
    provider_refund_id: Optional[str] = None
    provider_refund_status: Optional[str] = None
    failure_reason: Optional[str] = None
    decision_reason: Optional[str] = None
    created_at: str
    updated_at: str
    captured_at: Optional[str] = None
    refunded_at: Optional[str] = None
    forfeited_at: Optional[str] = None


class DepositActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crawler_id: str = Field(..., min_length=2, max_length=128)
    challenge_id: Optional[str] = Field(default=None)


class PolicyAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(..., min_length=8, max_length=128)
    deposit_id: str = Field(..., min_length=8, max_length=128)
    crawler_id: str = Field(..., min_length=2, max_length=128)
    policy_version: str = Field(..., min_length=1, max_length=32)
    agree_to_terms: bool


class ViolationEntry(BaseModel):
    timestamp: str
    path: str
    method: str
    reason_code: str
    reason: str
    disqualifying: bool


class SessionRecord(BaseModel):
    session_id: str
    crawler_id: str
    challenge_id: str
    deposit_id: str
    policy_version: str
    policy_accepted_at: str
    payment_reference: str
    payment_mode: PaymentMode
    token_hash: str
    status: SessionStatus
    created_at: str
    expires_at: str
    completed_at: Optional[str] = None
    revoked_at: Optional[str] = None
    total_requests: int = 0
    allowed_requests: int = 0
    rejected_requests: int = 0
    total_violations: int = 0
    disallowed_path_violations: int = 0
    rate_limit_violations: int = 0
    budget_violations: int = 0
    disqualified: bool = False
    disqualification_reasons: list[str] = Field(default_factory=list)
    violations: list[ViolationEntry] = Field(default_factory=list)


class SessionIssueResponse(BaseModel):
    session_id: str
    crawler_id: str
    deposit_id: str
    policy_version: str
    policy_accepted_at: str
    payment_mode: PaymentMode
    simulated_payment: bool
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    status: SessionStatus
    created_at: str
    expires_at: str
    max_requests_per_session: int
    rate_limit_requests: int
    rate_limit_window_seconds: float


class SessionStatusResponse(BaseModel):
    session_id: str
    crawler_id: str
    challenge_id: str
    deposit_id: str
    policy_version: str
    policy_accepted_at: str
    payment_mode: PaymentMode
    status: SessionStatus
    created_at: str
    expires_at: str
    completed_at: Optional[str] = None
    revoked_at: Optional[str] = None
    total_requests: int
    allowed_requests: int
    rejected_requests: int
    total_violations: int
    disallowed_path_violations: int
    rate_limit_violations: int
    budget_violations: int
    disqualified: bool
    disqualification_reasons: list[str]
    violations: list[ViolationEntry]


class SessionCompleteResponse(BaseModel):
    session: SessionStatusResponse
    deposit: DepositResponse
    refund_eligible: bool
    decision_reason: str


class SessionRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: Optional[str] = None
    reason: str = Field(default="Revoked by request", max_length=256)


class PolicyDecision(BaseModel):
    allowed: bool
    http_status: int
    reason_code: str
    reason: str
    violation: bool
    disqualifying: bool
    policy_version: str
    normalized_path: str
    remaining_session_budget: int
    remaining_window_requests: int
    retry_after_seconds: float = 0.0


class AuditEvent(BaseModel):
    event_id: str
    timestamp: str
    event_type: str
    crawler_id: Optional[str] = None
    challenge_id: Optional[str] = None
    session_id: Optional[str] = None
    deposit_id: Optional[str] = None
    path: Optional[str] = None
    method: Optional[str] = None
    allowed: Optional[bool] = None
    reason_code: Optional[str] = None
    policy_version: Optional[str] = None
    payment_mode: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class AuditSummaryResponse(BaseModel):
    scope: Literal["global", "session"]
    scoped_session_id: Optional[str] = None
    scoped_crawler_id: Optional[str] = None
    total_events: int
    total_gateway_requests: int
    allowed_gateway_requests: int
    rejected_gateway_requests: int
    policy_violations: int
    disqualifying_violations: int
    deposits_by_state: dict[str, int]
    payments_by_status: dict[str, int]
    decisions_by_status: dict[str, int]
    refunds_by_status: dict[str, int]
    sessions_by_status: dict[str, int]
    violation_counts_by_reason: dict[str, int]

