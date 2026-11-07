"""Pydantic models, enums, and state transition definitions for SARA."""

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


class PricingResource(BaseModel):
    """A protected resource sold under a pricing tier."""

    path: str
    title: str
    category: str


class PricingTier(BaseModel):
    """Publisher-defined content access tier. The gateway, never the crawler, picks the price."""

    tier_id: str = Field(..., pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    name: str
    description: str
    price: str
    currency: str = "USD"
    # Higher rank covers lower ranks: a Premium deposit also opens Standard and Public content.
    rank: int = Field(..., ge=1, le=100)
    categories: list[str] = Field(default_factory=list)
    path_prefix: str
    # Further path prefixes priced at this tier (for example /news for the Public tier).
    additional_path_prefixes: list[str] = Field(default_factory=list)
    resources: list[PricingResource] = Field(default_factory=list)


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
    # Tier-based deposit pricing by resource path; paths outside every tier use deposit_amount.
    pricing_tiers: list[PricingTier] = Field(default_factory=list)


class ChallengeDepositInfo(BaseModel):
    amount: str
    currency: str
    refundable: bool


class ChallengePricingInfo(BaseModel):
    """The pricing tier the gateway applied to the requested resource."""

    tier_id: str
    tier_name: str
    price: str
    currency: str
    resource: str


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
    pricing: Optional[ChallengePricingInfo] = None
    # Who the gateway believes the agent is (signed requests), and how that set the price.
    identity: Optional[dict[str, Any]] = None
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
    pricing_tier: Optional[str] = None
    pricing_tier_name: Optional[str] = None


class DepositCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(..., min_length=8, max_length=128)
    crawler_id: str = Field(..., min_length=2, max_length=128)
    # With the person's consent, save the payment method with PayPal on this payment...
    save_payment_method: bool = False
    # ...or pay with a method saved earlier (an opaque pm_ ID; card details never reach the agent).
    payment_method_id: Optional[str] = Field(default=None, max_length=64)


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
    # Pricing tier of the resource named in the 402 challenge (None = base site deposit).
    pricing_tier: Optional[str] = None
    pricing_tier_name: Optional[str] = None
    requested_resource: Optional[str] = None
    payment_method_id: Optional[str] = None
    payment_method_label: Optional[str] = None

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
    # Pricing tier of the resource named in the 402 challenge (None = base site deposit).
    pricing_tier: Optional[str] = None
    pricing_tier_name: Optional[str] = None
    requested_resource: Optional[str] = None
    payment_method_id: Optional[str] = None
    payment_method_label: Optional[str] = None


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
    provider_capture_id: Optional[str] = None
    provider_authorization_id: Optional[str] = None
    provider_refund_id: Optional[str] = None
    pricing_tier: Optional[str] = None
    pricing_tier_name: Optional[str] = None
    requested_resource: Optional[str] = None


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
    source: Literal["paypal_mcp", "sara"]
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    duration_ms: int
    summary: str
    error: Optional[str] = None
    preview: Optional[str] = None


class PayPalVerificationCheck(BaseModel):
    label: str
    sara: str
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


class PayPalOrderVerificationResponse(BaseModel):
    """A commerce order (pay-per-access or products) checked against PayPal's records."""

    order_id: str
    kind: Literal["access", "product"]
    order_status: str
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
    persona: Literal["compliant", "rogue", "assistant"]
    # Content access tier for the compliant crawler (validated against the site's tiers).
    tier: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    # Personal agent (persona "assistant"): the person's request and spending mandate.
    task: Optional[str] = Field(default=None, min_length=8, max_length=800)
    budget: Optional[str] = Field(default=None, pattern=r"^\d{1,4}(\.\d{1,2})?$")
    approval_threshold: Optional[str] = Field(default=None, pattern=r"^\d{1,4}(\.\d{1,2})?$")
    allowed_types: Optional[list[Literal["deposit", "access", "subscription", "product"]]] = Field(default=None, max_length=4)
    max_per_purchase: Optional[str] = Field(default=None, pattern=r"^\d{1,4}(\.\d{1,2})?$")
    allowed_merchants: Optional[list[str]] = Field(default=None, max_length=10)
    # A second AI call reviews every purchase against the person's request (never looser).
    purchase_review: bool = True
    # The person consents to PayPal saving their payment method on the agent's first payment,
    # so later payments in the task need no one present.
    save_payment_method: bool = False


class DemoDecisionRequest(BaseModel):
    """The person's answer to a purchase the personal agent asked to make."""

    model_config = ConfigDict(extra="forbid")

    approve: bool


class DemoRunEvent(BaseModel):
    seq: int
    at: str
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)


class DemoRunResponse(BaseModel):
    run_id: str
    driver: Literal["ai", "scripted"]
    persona: Literal["compliant", "rogue", "assistant"]
    status: Literal["running", "awaiting_approval", "awaiting_decision", "completed", "failed", "cancelled"]
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
    pricing_tier: Optional[dict[str, Any]] = None
    # Personal agent runs: the task, the purchase awaiting the person, and the mandate ledger.
    task: Optional[str] = None
    pending_decision: Optional[dict[str, Any]] = None
    mandate: Optional[dict[str, Any]] = None
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
    # Public PayPal client ID for the JS SDK buttons (Sandbox mode only; safe to expose).
    paypal_js_client_id: Optional[str] = None
    access: Literal["admin", "public", "none"]
    can_start_runs: bool
    runs_remaining_this_hour: int
    active_run_id: Optional[str] = None
    recent_runs: list[dict[str, Any]] = Field(default_factory=list)
    pricing_tiers: list[PricingTier] = Field(default_factory=list)


class DemoRequestRow(BaseModel):
    """One gateway request, joined with the deposit that paid for it (from the audit log)."""

    request_id: str
    timestamp: str
    crawler_id: Optional[str] = None
    resource: Optional[str] = None
    pricing_tier: Optional[str] = None
    pricing_tier_name: Optional[str] = None
    deposit_id: Optional[str] = None
    deposit_amount: Optional[str] = None
    currency: Optional[str] = None
    http_status: Optional[int] = None
    payment_status: Optional[str] = None
    deposit_state: Optional[str] = None
    access_decision: Literal["granted", "payment_required", "denied"]
    reason_code: Optional[str] = None
    simulated_payment: Optional[bool] = None
    provider_order_id: Optional[str] = None


class DemoRequestHistoryResponse(BaseModel):
    rows: list[DemoRequestRow]
    generated_at: str


# --- Agent commerce: pay-per-access, subscriptions, products -----------------------------

_PRICE_PATTERN = r"^\d{1,6}(\.\d{1,2})?$"
_ID_PATTERN = r"^[a-z][a-z0-9_-]{1,63}$"


class ProtectedPage(BaseModel):
    """Content served only to a buyer whose token covers its path."""

    title: str
    content_type: str = "text/html; charset=utf-8"
    body: str


class AccessItem(BaseModel):
    """A fixed-price, non-refundable item: one payment unlocks one resource (video, dataset, audio)."""

    item_id: str = Field(..., pattern=_ID_PATTERN)
    kind: Literal["video", "audio", "dataset", "report", "article"]
    title: str
    description: str
    price: str = Field(..., pattern=_PRICE_PATTERN)
    path: str = Field(..., pattern=r"^/media/[a-z0-9/_-]+$")
    content_type: str = "text/html; charset=utf-8"
    body: str


class SubscriptionPlan(BaseModel):
    """A recurring plan billed through PayPal Subscriptions; unlocks every page under path_prefix."""

    plan_id: str = Field(..., pattern=_ID_PATTERN)
    name: str
    description: str
    price: str = Field(..., pattern=_PRICE_PATTERN)
    interval_unit: Literal["DAY", "WEEK", "MONTH", "YEAR"] = "MONTH"
    interval_count: int = Field(default=1, ge=1, le=12)
    path_prefix: str = Field(..., pattern=r"^/members$|^/members/[a-z0-9/_-]+$")
    benefits: list[str] = Field(default_factory=list)


class Product(BaseModel):
    """A product an agent can buy: digital (download link) or physical (shipped)."""

    sku: str = Field(..., pattern=_ID_PATTERN)
    name: str
    description: str
    price: str = Field(..., pattern=_PRICE_PATTERN)
    fulfillment: Literal["digital", "physical"]
    shipping: str = Field(default="0.00", pattern=_PRICE_PATTERN)
    download_path: Optional[str] = Field(default=None, pattern=r"^/downloads/[a-z0-9/_-]+$")


class CommerceCatalogFile(BaseModel):
    merchant_name: str
    catalog_version: str = "1.0"
    access_items: list[AccessItem] = Field(default_factory=list)
    subscription_plans: list[SubscriptionPlan] = Field(default_factory=list)
    members_pages: dict[str, ProtectedPage] = Field(default_factory=dict)
    products: list[Product] = Field(default_factory=list)
    downloads: dict[str, ProtectedPage] = Field(default_factory=dict)


class OrderLine(BaseModel):
    ref: str  # item_id or sku
    name: str
    unit_price: str
    quantity: int
    category: Literal["DIGITAL_GOODS", "PHYSICAL_GOODS"]


class CommerceOrderRecord(BaseModel):
    order_id: str
    kind: Literal["access", "product"]
    agent_id: str
    status: Literal["CREATED", "APPROVAL_REQUIRED", "PAID", "FAILED", "CANCELLED", "REFUNDED"]
    amount: str
    currency: str
    lines: list[OrderLine]
    shipping: str = "0.00"
    purpose: Optional[str] = None
    payment_mode: PaymentMode
    simulated: bool
    provider_order_id: Optional[str] = None
    provider_capture_id: Optional[str] = None
    approval_url: Optional[str] = None
    fulfillment_status: Optional[Literal["DELIVERED", "PROCESSING", "SHIPPED", "PENDING_PAYMENT"]] = None
    grants: list[str] = Field(default_factory=list)
    access_token_hash: Optional[str] = None
    created_at: str
    paid_at: Optional[str] = None
    failure_reason: Optional[str] = None
    provider_refund_id: Optional[str] = None
    refunded_at: Optional[str] = None
    tracking: list[dict[str, str]] = Field(default_factory=list)
    dispute_id: Optional[str] = None
    dispute_status: Optional[str] = None
    # Set when a verified PayPal webhook confirmed the payment (not only the agent's own call).
    confirmed_by_webhook: bool = False
    payment_method_id: Optional[str] = None
    payment_method_label: Optional[str] = None
    # The agent pays with a saved method or the Sandbox test card, attached at capture.
    agent_pays: bool = False
    save_payment_method: bool = False


class SubscriptionRecord(BaseModel):
    subscription_ref: str
    plan_id: str
    plan_name: str
    agent_id: str
    status: Literal["APPROVAL_PENDING", "APPROVED", "ACTIVE", "SUSPENDED", "CANCELLED", "EXPIRED", "FAILED"]
    amount: str
    currency: str
    interval_unit: str
    interval_count: int
    path_prefix: str
    purpose: Optional[str] = None
    payment_mode: PaymentMode
    simulated: bool
    provider_plan_id: Optional[str] = None
    provider_subscription_id: Optional[str] = None
    approval_url: Optional[str] = None
    next_billing_time: Optional[str] = None
    access_token_hash: Optional[str] = None
    created_at: str
    activated_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    failure_reason: Optional[str] = None
    payments_received: int = 0
    last_payment_at: Optional[str] = None


class CheckoutLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(..., pattern=_ID_PATTERN)
    quantity: int = Field(default=1, ge=1, le=10)


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=3, max_length=128)
    offer_type: Literal["access", "product"]
    item_id: Optional[str] = Field(default=None, pattern=_ID_PATTERN)
    items: list[CheckoutLineRequest] = Field(default_factory=list, max_length=10)
    purpose: Optional[str] = Field(default=None, max_length=300)
    # "human": a person pays with PayPal's buttons and approves at PayPal (no test-card autopay).
    buyer: Literal["agent", "human"] = "agent"
    save_payment_method: bool = False
    payment_method_id: Optional[str] = Field(default=None, max_length=64)


class AgentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=3, max_length=128)


class RefundOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="Refunded by the merchant", max_length=120)


class TrackingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking_number: str = Field(..., min_length=4, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    carrier: str = Field(..., min_length=2, max_length=64, pattern=r"^[A-Z0-9_]+$")


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=3, max_length=128)
    plan_id: str = Field(..., pattern=_ID_PATTERN)
    purpose: Optional[str] = Field(default=None, max_length=300)


class CommerceOrderResponse(BaseModel):
    order_id: str
    kind: Literal["access", "product"]
    agent_id: str
    status: str
    amount: str
    currency: str
    lines: list[OrderLine]
    shipping: str
    payment_mode: PaymentMode
    simulated: bool
    provider_order_id: Optional[str] = None
    provider_capture_id: Optional[str] = None
    approval_url: Optional[str] = None
    fulfillment_status: Optional[str] = None
    grants: list[str] = Field(default_factory=list)
    created_at: str
    paid_at: Optional[str] = None
    failure_reason: Optional[str] = None
    provider_refund_id: Optional[str] = None
    refunded_at: Optional[str] = None
    tracking: list[dict[str, str]] = Field(default_factory=list)
    dispute_status: Optional[str] = None
    confirmed_by_webhook: bool = False
    payment_method_id: Optional[str] = None
    payment_method_label: Optional[str] = None
    # Returned once, on the call that first confirms payment. Stored only as a hash.
    access_token: Optional[str] = None


class SubscriptionResponse(BaseModel):
    subscription_ref: str
    plan_id: str
    plan_name: str
    agent_id: str
    status: str
    amount: str
    currency: str
    interval_unit: str
    interval_count: int
    path_prefix: str
    payment_mode: PaymentMode
    simulated: bool
    provider_subscription_id: Optional[str] = None
    approval_url: Optional[str] = None
    next_billing_time: Optional[str] = None
    created_at: str
    activated_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    failure_reason: Optional[str] = None
    payments_received: int = 0
    last_payment_at: Optional[str] = None
    access_token: Optional[str] = None
