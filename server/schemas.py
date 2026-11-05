"""Pydantic models, enums, and state transition definitions for CrawlerTrust."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PaymentMode(str, Enum):
    MOCK = "mock"
    PAYPAL_SANDBOX = "paypal_sandbox"


class SettlementMode(str, Enum):
    """How a deposit is settled with the payment provider."""

    REFUND = "refund"  # capture up front, refund compliant sessions
    AUTHORIZATION = "authorization"  # hold funds, void for compliant sessions, capture on forfeiture


class PaymentStatus(str, Enum):
    """Status of the inbound deposit payment transaction."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    AUTHORIZED = "AUTHORIZED"  # funds on hold (authorization settlement mode)
    CAPTURED = "CAPTURED"
    VOIDED = "VOIDED"  # hold released without capturing funds
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
    NOT_APPLICABLE_RELEASED = "NOT_APPLICABLE_RELEASED"  # hold voided; nothing to refund


class DepositState(str, Enum):
    """High-level composite deposit lifecycle state."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    RELEASED = "RELEASED"
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
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
    },
    PaymentStatus.APPROVED: {
        PaymentStatus.APPROVED,
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.FAILED,
    },
    PaymentStatus.AUTHORIZED: {
        PaymentStatus.AUTHORIZED,
        PaymentStatus.CAPTURED,
        PaymentStatus.VOIDED,
        PaymentStatus.FAILED,
    },
    PaymentStatus.CAPTURED: {PaymentStatus.CAPTURED},
    PaymentStatus.VOIDED: {PaymentStatus.VOIDED},
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
        RefundTransactionStatus.NOT_APPLICABLE_RELEASED,
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
    RefundTransactionStatus.NOT_APPLICABLE_RELEASED: {
        RefundTransactionStatus.NOT_APPLICABLE_RELEASED
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
    # Authorization-hold settlement (records persisted before this field default to refund).
    settlement_mode: SettlementMode = SettlementMode.REFUND
    provider_authorization_id: Optional[str] = None
    provider_authorization_status: Optional[str] = None
    released_at: Optional[str] = None

    @property
    def is_funded(self) -> bool:
        """True when the deposit is captured, or held by a verified authorization."""
        if self.payment_status == PaymentStatus.CAPTURED:
            return bool(self.provider_capture_id)
        if self.payment_status == PaymentStatus.AUTHORIZED:
            return bool(self.provider_authorization_id)
        return False

    @property
    def payment_reference(self) -> Optional[str]:
        """Provider reference that backs the session: capture ID or authorization ID."""
        return self.provider_capture_id or self.provider_authorization_id


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
    settlement_mode: SettlementMode = SettlementMode.REFUND
    provider_authorization_id: Optional[str] = None
    provider_authorization_status: Optional[str] = None
    released_at: Optional[str] = None


class PayPalApprovalCallbackResponse(BaseModel):
    outcome: Literal[
        "CAPTURED", "AUTHORIZED", "CANCELLED", "ALREADY_CAPTURED", "ALREADY_AUTHORIZED"
    ]
    deposit_id: str
    payment_status: PaymentStatus
    deposit_state: DepositState
    provider_order_id: str
    provider_capture_id: Optional[str] = None
    provider_authorization_id: Optional[str] = None
    captured_amount: Optional[str] = None
    currency: str


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
    # Log-safe form of the path the crawler sent; set when normalized_path cannot
    # represent it (for example, rejected traversal attempts).
    requested_path: Optional[str] = None


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


class ExplanationFinding(BaseModel):
    statement: str
    evidence: list[str]


class EvidenceItem(BaseModel):
    evidence_id: str
    event_id: str
    timestamp: str
    event_type: str
    path: Optional[str] = None
    reason_code: Optional[str] = None
    allowed: Optional[bool] = None


class DepositExplanationResponse(BaseModel):
    deposit_id: str
    session_id: Optional[str] = None
    crawler_id: str
    deposit_state: DepositState
    policy_decision: PolicyDecisionStatus
    generated_by: Literal["claude", "openai", "template"]
    model: Optional[str] = None
    headline: str
    summary: str
    findings: list[ExplanationFinding]
    evidence: list[EvidenceItem]
    omitted_events: int = 0
    notice: Optional[str] = None
    generated_at: str


class PolicyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: str = Field(..., min_length=20, max_length=8000)
    site_id: Optional[str] = Field(default=None, max_length=64)
    site_name: Optional[str] = Field(default=None, max_length=128)


class PolicyDraftResponse(BaseModel):
    generated_by: Literal["claude", "openai"] = "claude"
    model: str
    valid: bool
    draft: dict[str, Any]
    validation_errors: list[str]
    assumptions: list[str]
    warnings: list[str]
    notice: str = (
        "Draft only. Review it, then save it to data/site_config.json and restart the "
        "gateway to apply. Bump policy_version when rules change."
    )


class DashboardKpis(BaseModel):
    sessions_total: int
    sessions_active: int
    deposits_total: int
    deposits_held: int
    deposits_returned: int
    deposits_forfeited: int
    deposits_needing_attention: int
    amount_returned: str
    amount_forfeited: str
    currency: str
    gateway_requests_allowed: int
    gateway_requests_rejected: int
    policy_violations: int
    violations_by_reason: dict[str, int]


class DashboardDepositRow(BaseModel):
    deposit_id: str
    crawler_id: str
    session_id: Optional[str] = None
    amount: str
    currency: str
    payment_mode: PaymentMode
    settlement_mode: SettlementMode
    deposit_state: DepositState
    payment_status: PaymentStatus
    policy_decision: PolicyDecisionStatus
    refund_status: RefundTransactionStatus
    created_at: str
    updated_at: str
    provider_order_id: Optional[str] = None


class DashboardSessionRow(BaseModel):
    session_id: str
    crawler_id: str
    deposit_id: str
    status: SessionStatus
    total_requests: int
    allowed_requests: int
    rejected_requests: int
    total_violations: int
    disqualified: bool
    created_at: str
    completed_at: Optional[str] = None


class DashboardEventRow(BaseModel):
    """Audit event without its details payload (details can hold checkout URLs)."""

    event_id: str
    timestamp: str
    event_type: str
    crawler_id: Optional[str] = None
    session_id: Optional[str] = None
    deposit_id: Optional[str] = None
    path: Optional[str] = None
    reason_code: Optional[str] = None
    allowed: Optional[bool] = None


class DashboardDataResponse(BaseModel):
    site_id: str
    site_name: str
    policy_version: str
    payment_mode: PaymentMode
    settlement_mode: SettlementMode
    ai_available: bool
    ai_provider: str
    ai_model: str
    access: Literal["admin", "public"]
    kpis: DashboardKpis
    deposits: list[DashboardDepositRow]
    sessions: list[DashboardSessionRow]
    events: list[DashboardEventRow]
    generated_at: str


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


# --- PayPal assistant (PayPal MCP server) ------------------------------------------------


class PayPalToolCallTrace(BaseModel):
    source: Literal["paypal_mcp", "crawlertrust"]
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    duration_ms: int
    summary: str
    error: Optional[str] = None
    preview: Optional[str] = None


class PayPalVerificationCheck(BaseModel):
    label: str
    crawlertrust: str
    paypal: str
    status: Literal["match", "mismatch", "info"]


class PayPalVerificationResponse(BaseModel):
    deposit_id: str
    deposit_state: DepositState
    settlement_mode: SettlementMode
    consistent: bool
    verdict: str
    checks: list[PayPalVerificationCheck]
    calls: list[PayPalToolCallTrace]
    mcp_server: str
    transport: Literal["local", "hosted"]
    checked_at: str


class PayPalAssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=600)


class PayPalAssistantResponse(BaseModel):
    answer: str
    generated_by: Literal["claude", "openai"]
    model: str
    calls: list[PayPalToolCallTrace]
    steps: int
    mcp_server: str
    transport: Literal["local", "hosted"]
    notice: str
    generated_at: str


class PayPalAssistantStatus(BaseModel):
    mcp_configured: bool
    mcp_unavailable_reason: Optional[str] = None
    mcp_transport: Literal["local", "hosted"]
    mcp_server: str
    read_only_tools: list[str]
    ai_available: bool
    ai_provider: str
    ai_model: str
    payment_mode: PaymentMode
    access: Literal["admin", "public"]
    questions_remaining_this_hour: Optional[int] = None


# --- Live demo runs ------------------------------------------------------------------------


class DemoRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: Literal["ai", "scripted"]
    persona: Literal["compliant", "rogue"]


class DemoRunEvent(BaseModel):
    seq: int
    at: str
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)


class DemoRunResponse(BaseModel):
    run_id: str
    driver: Literal["ai", "scripted"]
    persona: Literal["compliant", "rogue"]
    status: Literal["running", "awaiting_approval", "completed", "failed", "cancelled"]
    crawler_id: str
    model: Optional[str] = None
    provider: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None
    approval_url: Optional[str] = None
    deposit: Optional[dict[str, Any]] = None
    session: Optional[dict[str, Any]] = None
    explanation: Optional[dict[str, Any]] = None
    final_text: Optional[str] = None
    error: Optional[str] = None
    usage: dict[str, Any] = Field(default_factory=dict)
    events: list[DemoRunEvent] = Field(default_factory=list)
    next_seq: int = 0


class DemoConfigResponse(BaseModel):
    site_name: str
    policy_version: str
    payment_mode: PaymentMode
    paypal_autopay: bool
    settlement_mode: SettlementMode
    deposit_amount: str
    currency: str
    ai_available: bool
    ai_provider: str
    ai_model: str
    paypal_mcp_configured: bool
    paypal_mcp_transport: Literal["local", "hosted"]
    demo_public: bool
    access: Literal["admin", "public", "none"]
    can_start_runs: bool
    runs_remaining_this_hour: int
    active_run_id: Optional[str] = None
    recent_runs: list[dict[str, Any]] = Field(default_factory=list)
