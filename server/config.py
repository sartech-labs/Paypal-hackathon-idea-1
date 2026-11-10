"""Configuration management and validated settings for SARA."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()


class Settings(BaseModel):
    """Validated application settings loaded from environment variables."""

    app_env: Literal["development", "test", "production"] = Field(default="development")
    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_public_url: str = Field(default="http://127.0.0.1:8000")
    # demo: the console is the landing page. gateway: "/" is proxied to the owner site and
    # the console stays at /console.
    sara_mode: Literal["demo", "gateway"] = Field(default="demo")

    payment_mode: Literal["mock", "paypal_sandbox"] = Field(default="mock")

    paypal_base_url: str = Field(default="https://api-m.sandbox.paypal.com")
    paypal_client_id: str = Field(default="")
    paypal_client_secret: str = Field(default="")
    paypal_currency: str = Field(default="USD")
    paypal_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    # Sandbox only: pay deposits with PayPal's Sandbox test card instead of buyer approval,
    # so AI agents (and demo viewers) can complete real Sandbox payments without a login.
    paypal_sandbox_autopay: bool = Field(default=False)
    # Webhook ID from the PayPal app's webhook settings; events are verified against it.
    paypal_webhook_id: str = Field(default="", max_length=64)

    # PayPal MCP server used by the publisher's PayPal assistant (read-only tools).
    # "local" runs PayPal's @paypal/mcp package over stdio; "hosted" connects to PayPal's
    # remote MCP server over SSE with the app's OAuth access token.
    paypal_mcp_enabled: bool = Field(default=True)
    paypal_mcp_transport: Literal["local", "hosted"] = Field(default="local")
    paypal_mcp_command: str = Field(default="npx -y @paypal/mcp@1.8.1", max_length=500)
    paypal_mcp_url: str = Field(default="https://mcp.sandbox.paypal.com/sse")
    paypal_mcp_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)

    # Base deposit for pages outside every pricing tier (for example /news).
    deposit_amount: Decimal = Field(default=Decimal("1.00"))
    # Tier prices, numbered from most to least valuable: tier1 = Premium, tier2 = Standard,
    # tier3 = Public. Unset values keep the price from data/site_config.json.
    tier1_price: Optional[Decimal] = Field(default=None)
    tier2_price: Optional[Decimal] = Field(default=None)
    tier3_price: Optional[Decimal] = Field(default=None)
    # How a deposit is settled: "refund" captures up front and refunds compliant
    # sessions; "authorization" places a hold that is voided for compliant
    # sessions and captured only on forfeiture.
    deposit_settlement: Literal["refund", "authorization"] = Field(default="refund")
    # Reverse-proxy mode: put the gateway in front of an existing website. Its robots.txt
    # rules are imported and enforced, and allowed requests are forwarded to it.
    origin_url: str = Field(default="", max_length=500)
    # Verified agent identity (signed requests, Web Bot Auth). Agents whose keys are in the
    # directory get a stable identity; a clean record earns a deposit discount, a kept
    # deposit gets the identity refused. Unsigned agents pay the full deposit.
    agent_directory_path: Path = Field(default=Path("data/agent_directory.json"))
    trusted_agent_discount: Decimal = Field(default=Decimal("0.50"), ge=Decimal("0"), le=Decimal("1"))
    flagged_agent_multiplier: Decimal = Field(default=Decimal("3"), ge=Decimal("1"), le=Decimal("100"))
    refuse_after_forfeits: int = Field(default=3, ge=1, le=1000)
    require_signed_agents: bool = Field(default=False)
    origin_timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    session_ttl_seconds: int = Field(default=1800, ge=1, le=86400)
    challenge_ttl_seconds: int = Field(default=600, ge=1, le=86400)

    max_requests_per_session: int = Field(default=10, ge=1, le=10000)
    rate_limit_requests: int = Field(default=3, ge=1, le=1000)
    rate_limit_window_seconds: float = Field(default=10.0, gt=0.0, le=3600.0)

    site_config_path: Path = Field(default=Path("data/site_config.json"))
    robots_config_path: Path = Field(default=Path("data/robots.json"))
    allowed_pages_path: Path = Field(default=Path("data/allowed_pages.json"))
    disallowed_pages_path: Path = Field(default=Path("data/disallowed_pages.json"))

    audit_log_path: Path = Field(default=Path("storage/audit.jsonl"))
    payments_db_path: Path = Field(default=Path("storage/payments_db.json"))
    sessions_db_path: Path = Field(default=Path("storage/sessions_db.json"))
    # Agent commerce: the offer catalog (pay-per-access, subscriptions, products) and the
    # store of orders, subscriptions and access grants.
    commerce_catalog_path: Path = Field(default=Path("data/commerce_catalog.json"))
    commerce_db_path: Path = Field(default=Path("storage/commerce_db.json"))

    admin_api_token: str = Field(default="")
    # Read-only publisher dashboard without the admin token (for demo deployments only).
    dashboard_public: bool = Field(default=False)
    # Owner-editable runtime configuration (target URL, deposits, rules), persisted here.
    gateway_config_path: Path = Field(default=Path("storage/gateway_config.json"))
    # Demo deployments only: let visitors apply target URL / deposit / rule changes without
    # the admin token. Public writes get SSRF checks, an origin allowlist, and a quota.
    demo_config_public: bool = Field(default=False)
    demo_origin_allowlist: str = Field(default="")
    config_max_writes_per_hour: int = Field(default=30, ge=1, le=1000)
    # Transparent edge: classify unsigned clients by User-Agent (a signature always counts).
    # A recognised browser User-Agent is human, everything else is an agent.
    detect_agent_user_agents: bool = Field(default=True)
    playground_max_runs_per_hour: int = Field(default=20, ge=1, le=1000)
    # Live demo page: when true, anyone may start crawler runs and ask the PayPal
    # assistant (read-only), within the hourly caps below. Admin token otherwise.
    demo_public: bool = Field(default=False)
    demo_max_runs_per_hour: int = Field(default=20, ge=1, le=1000)
    demo_max_questions_per_hour: int = Field(default=30, ge=1, le=1000)
    log_level: str = Field(default="INFO")

    # Publisher-side AI. Provider "auto" uses Claude when Anthropic credentials resolve
    # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or a profile), otherwise OpenAI when
    # OPENAI_API_KEY is set. AI_MODEL defaults per provider when left empty.
    ai_enabled: bool = Field(default=True)
    ai_provider: Literal["auto", "anthropic", "openai"] = Field(default="auto")
    ai_model: Optional[str] = Field(default=None, max_length=128)
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(default="medium")
    ai_refusal_fallback: bool = Field(default=True)
    ai_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)

    @field_validator("deposit_amount", mode="before")
    @classmethod
    def validate_deposit_amount(cls, value: object) -> Decimal:
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid deposit_amount: {value!r}") from exc
        if amount <= Decimal("0.00"):
            raise ValueError("deposit_amount must be strictly positive (> 0.00)")
        return amount

    @field_validator("tier1_price", "tier2_price", "tier3_price", mode="before")
    @classmethod
    def validate_tier_price(cls, value: object) -> Optional[Decimal]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            amount = Decimal(str(value).strip()).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid tier price: {value!r}") from exc
        if amount <= Decimal("0.00"):
            raise ValueError("Tier prices must be strictly positive (> 0.00)")
        return amount

    @property
    def tier_prices(self) -> list[Optional[Decimal]]:
        """Tier price overrides, most valuable tier first."""
        return [self.tier1_price, self.tier2_price, self.tier3_price]

    @field_validator("origin_url")
    @classmethod
    def _validate_origin_url(cls, value: str) -> str:
        value = (value or "").strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("ORIGIN_URL must start with http:// or https://")
        return value

    @field_validator("paypal_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if len(cleaned) != 3 or not cleaned.isalpha():
            raise ValueError("paypal_currency must be a 3-letter ISO currency code")
        return cleaned

    @field_validator("paypal_base_url")
    @classmethod
    def validate_paypal_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith("https://"):
            raise ValueError("paypal_base_url must use https://")
        return cleaned

    @field_validator("paypal_mcp_url")
    @classmethod
    def validate_paypal_mcp_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith("https://"):
            raise ValueError("paypal_mcp_url must use https://")
        return cleaned

    @field_validator("ai_model", mode="before")
    @classmethod
    def normalize_ai_model(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("app_public_url")
    @classmethod
    def validate_app_public_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        parsed = urlsplit(cleaned)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "app_public_url must be an absolute HTTP(S) URL without credentials, query, or fragment"
            )
        return cleaned

    @model_validator(mode="after")
    def validate_production_constraints(self) -> "Settings":
        if self.app_env == "production" and not self.admin_api_token.strip():
            raise ValueError("ADMIN_API_TOKEN must be configured when APP_ENV=production")
        if self.paypal_sandbox_autopay and not self.paypal_is_sandbox:
            raise ValueError("PAYPAL_SANDBOX_AUTOPAY is only allowed with the PayPal Sandbox API")
        return self

    @property
    def paypal_is_sandbox(self) -> bool:
        return urlsplit(self.paypal_base_url).hostname == "api-m.sandbox.paypal.com"


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable, rejecting ambiguous values."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def load_settings_from_env() -> Settings:
    """Construct Settings from current environment variables."""
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),  # type: ignore[arg-type]
        app_host=os.getenv("APP_HOST", "127.0.0.1"),
        app_port=int(os.getenv("APP_PORT", "8000")),
        # Render injects RENDER_EXTERNAL_URL for web services; use it when no
        # explicit public URL is configured so PayPal callbacks reach the app.
        app_public_url=(
            os.getenv("APP_PUBLIC_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or "http://127.0.0.1:8000"
        ),
        sara_mode=os.getenv("SARA_MODE", "demo").strip().lower() or "demo",  # type: ignore[arg-type]
        payment_mode=os.getenv("PAYMENT_MODE", "mock"),  # type: ignore[arg-type]
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_currency=os.getenv("PAYPAL_CURRENCY", "USD"),
        paypal_timeout_seconds=float(os.getenv("PAYPAL_TIMEOUT_SECONDS", "15.0")),
        paypal_sandbox_autopay=_env_bool("PAYPAL_SANDBOX_AUTOPAY", False),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
        paypal_mcp_enabled=_env_bool("PAYPAL_MCP_ENABLED", True),
        paypal_mcp_transport=os.getenv("PAYPAL_MCP_TRANSPORT", "local").strip().lower() or "local",  # type: ignore[arg-type]
        paypal_mcp_command=os.getenv("PAYPAL_MCP_COMMAND", "").strip() or "npx -y @paypal/mcp@1.8.1",
        paypal_mcp_url=os.getenv("PAYPAL_MCP_URL", "").strip() or "https://mcp.sandbox.paypal.com/sse",
        paypal_mcp_timeout_seconds=float(os.getenv("PAYPAL_MCP_TIMEOUT_SECONDS", "60")),
        deposit_amount=Decimal(os.getenv("DEPOSIT_AMOUNT", "1.00")),
        tier1_price=os.getenv("TIER1_PRICE") or None,  # type: ignore[arg-type]
        tier2_price=os.getenv("TIER2_PRICE") or None,  # type: ignore[arg-type]
        tier3_price=os.getenv("TIER3_PRICE") or None,  # type: ignore[arg-type]
        deposit_settlement=os.getenv("DEPOSIT_SETTLEMENT", "refund"),  # type: ignore[arg-type]
        origin_url=os.getenv("ORIGIN_URL", ""),
        agent_directory_path=Path(os.getenv("AGENT_DIRECTORY_PATH", "data/agent_directory.json")),
        trusted_agent_discount=Decimal(os.getenv("TRUSTED_AGENT_DISCOUNT", "0.50")),
        flagged_agent_multiplier=Decimal(os.getenv("FLAGGED_AGENT_MULTIPLIER", "3")),
        refuse_after_forfeits=int(os.getenv("REFUSE_AFTER_FORFEITS", "3")),
        require_signed_agents=_env_bool("REQUIRE_SIGNED_AGENTS", False),
        origin_timeout_seconds=float(os.getenv("ORIGIN_TIMEOUT_SECONDS", "10")),
        session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "1800")),
        challenge_ttl_seconds=int(os.getenv("CHALLENGE_TTL_SECONDS", "600")),
        max_requests_per_session=int(os.getenv("MAX_REQUESTS_PER_SESSION", "10")),
        rate_limit_requests=int(os.getenv("RATE_LIMIT_REQUESTS", "3")),
        rate_limit_window_seconds=float(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "10")),
        site_config_path=Path(os.getenv("SITE_CONFIG_PATH", "data/site_config.json")),
        robots_config_path=Path(os.getenv("ROBOTS_CONFIG_PATH", "data/robots.json")),
        allowed_pages_path=Path(os.getenv("ALLOWED_PAGES_PATH", "data/allowed_pages.json")),
        disallowed_pages_path=Path(
            os.getenv("DISALLOWED_PAGES_PATH", "data/disallowed_pages.json")
        ),
        audit_log_path=Path(os.getenv("AUDIT_LOG_PATH", "storage/audit.jsonl")),
        payments_db_path=Path(os.getenv("PAYMENTS_DB_PATH", "storage/payments_db.json")),
        sessions_db_path=Path(os.getenv("SESSIONS_DB_PATH", "storage/sessions_db.json")),
        commerce_catalog_path=Path(os.getenv("COMMERCE_CATALOG_PATH", "data/commerce_catalog.json")),
        commerce_db_path=Path(os.getenv("COMMERCE_DB_PATH", "storage/commerce_db.json")),
        admin_api_token=os.getenv("ADMIN_API_TOKEN", ""),
        dashboard_public=_env_bool("DASHBOARD_PUBLIC", False),
        gateway_config_path=Path(os.getenv("GATEWAY_CONFIG_PATH", "storage/gateway_config.json")),
        demo_config_public=_env_bool("DEMO_CONFIG_PUBLIC", False),
        demo_origin_allowlist=os.getenv("DEMO_ORIGIN_ALLOWLIST", ""),
        config_max_writes_per_hour=int(os.getenv("CONFIG_MAX_WRITES_PER_HOUR", "30")),
        detect_agent_user_agents=_env_bool("DETECT_AGENT_USER_AGENTS", True),
        playground_max_runs_per_hour=int(os.getenv("PLAYGROUND_MAX_RUNS_PER_HOUR", "20")),
        demo_public=_env_bool("DEMO_PUBLIC", False),
        demo_max_runs_per_hour=int(os.getenv("DEMO_MAX_RUNS_PER_HOUR", "20")),
        demo_max_questions_per_hour=int(os.getenv("DEMO_MAX_QUESTIONS_PER_HOUR", "30")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        ai_enabled=_env_bool("AI_ENABLED", True),
        ai_provider=os.getenv("AI_PROVIDER", "auto").strip().lower() or "auto",  # type: ignore[arg-type]
        ai_model=os.getenv("AI_MODEL"),
        ai_effort=os.getenv("AI_EFFORT", "medium"),  # type: ignore[arg-type]
        ai_refusal_fallback=_env_bool("AI_REFUSAL_FALLBACK", True),
        ai_timeout_seconds=float(os.getenv("AI_TIMEOUT_SECONDS", "120")),
    )
