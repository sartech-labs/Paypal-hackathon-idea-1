"""Configuration management and validated settings for CrawlerTrust."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
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

    payment_mode: Literal["mock", "paypal_sandbox"] = Field(default="mock")

    paypal_base_url: str = Field(default="https://api-m.sandbox.paypal.com")
    paypal_client_id: str = Field(default="")
    paypal_client_secret: str = Field(default="")
    paypal_currency: str = Field(default="USD")
    paypal_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)

    deposit_amount: Decimal = Field(default=Decimal("0.01"))
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

    admin_api_token: str = Field(default="")
    log_level: str = Field(default="INFO")

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
        return self


def load_settings_from_env() -> Settings:
    """Construct Settings from current environment variables."""
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),  # type: ignore[arg-type]
        app_host=os.getenv("APP_HOST", "127.0.0.1"),
        app_port=int(os.getenv("APP_PORT", "8000")),
        app_public_url=os.getenv("APP_PUBLIC_URL", "http://127.0.0.1:8000"),
        payment_mode=os.getenv("PAYMENT_MODE", "mock"),  # type: ignore[arg-type]
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_currency=os.getenv("PAYPAL_CURRENCY", "USD"),
        paypal_timeout_seconds=float(os.getenv("PAYPAL_TIMEOUT_SECONDS", "15.0")),
        deposit_amount=Decimal(os.getenv("DEPOSIT_AMOUNT", "0.01")),
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
        admin_api_token=os.getenv("ADMIN_API_TOKEN", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
