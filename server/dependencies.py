"""Application service container and FastAPI dependencies."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from server.config import Settings
from server.core.identity import IdentityService
from server.core.security import constant_time_equals
from server.core.sessions import SessionManager
from server.schemas import PaymentMode, SessionRecord
from server.services.audit_logger import AuditLogger
from server.services.content_service import ContentService
from server.services.deposit_manager import DepositManager
from server.services.payment_service import (
    MockPaymentProvider,
    PaymentProvider,
    PayPalSandboxProvider,
)
from server.services.policy_engine import PolicyEngine
from server.services.rate_limiter import SlidingWindowRateLimiter


class AppContainer:
    """Holds initialized services for a FastAPI application instance."""

    def __init__(
        self,
        settings: Settings,
        payment_provider: Optional[PaymentProvider] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
    ) -> None:
        self.settings = settings
        self.content_service = ContentService(settings)
        self.site_config = self.content_service.site_config
        self.identity_service = IdentityService(self.site_config)
        self.audit_logger = AuditLogger(settings.audit_log_path)
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.policy_engine = PolicyEngine(
            site_config=self.site_config,
            rate_limiter=self.rate_limiter,
            known_allowed_pages=self.content_service.allowed_page_paths,
        )
        if payment_provider is not None:
            self.payment_provider = payment_provider
        elif settings.payment_mode == PaymentMode.PAYPAL_SANDBOX.value:
            self.payment_provider = PayPalSandboxProvider(settings)
        else:
            self.payment_provider = MockPaymentProvider()

        self.deposit_manager = DepositManager(
            payments_db_path=settings.payments_db_path,
            site_config=self.site_config,
            payment_provider=self.payment_provider,
            audit_logger=self.audit_logger,
        )
        self.session_manager = SessionManager(
            sessions_db_path=settings.sessions_db_path,
            site_config=self.site_config,
        )

    def load_persisted_state(self) -> None:
        """Load audit log, payment/challenge records, and session snapshots from disk."""
        self.audit_logger.load_from_disk()
        self.deposit_manager.load_from_disk()
        self.session_manager.load_from_disk()


def get_container(request: Request) -> AppContainer:
    """Retrieve the AppContainer from FastAPI app.state."""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("Application container is not initialized")
    return container


def extract_optional_bearer_token(
    authorization: Optional[str] = Header(default=None),
) -> Optional[str]:
    """
    Extract bearer token from Authorization header if present.
    Raises HTTP 401 if an Authorization header is present but malformed.
    """
    if authorization is None:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_authorization_header",
                "message": "Authorization header must use 'Bearer <crawler_token>' format.",
            },
        )
    return parts[1].strip()


def is_valid_admin_token(
    container: AppContainer,
    x_admin_token: Optional[str],
) -> bool:
    """
    Verify X-Admin-Token against configured ADMIN_API_TOKEN in constant time.
    Never accepts an empty or hardcoded default token.
    """
    configured = container.settings.admin_api_token.strip()
    if not configured or not x_admin_token:
        return False
    return constant_time_equals(configured, x_admin_token.strip())


async def require_authenticated_session(
    container: AppContainer = Depends(get_container),
    token: Optional[str] = Depends(extract_optional_bearer_token),
) -> SessionRecord:
    """Require a known session bearer token (raises 401 if missing or unknown)."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_credentials",
                "message": "Bearer token is required for this endpoint.",
            },
        )
    session = await container.session_manager.authenticate_token(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_credentials",
                "message": "Invalid crawler bearer token.",
            },
        )
    return session

