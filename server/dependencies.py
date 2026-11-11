"""Application service container and FastAPI dependencies."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.web_bot_auth import WebBotAuth
from server.config import Settings
from server.core.agent_identity import AgentDirectory
from server.core.identity import IdentityService
from server.core.quotas import HourlyQuota
from server.core.security import constant_time_equals
from server.core.sessions import SessionManager
from server.schemas import (
    PaymentMode,
    RuntimeConfigResponse,
    RuntimeConfigUpdate,
    SessionRecord,
    SettlementMode,
)
from server.services.ai_client import TextGenerator, build_text_generator
from server.services.audit_logger import AuditLogger
from server.services.commerce import CommerceCatalog, CommerceManager
from server.services.content_service import ContentService
from server.services.demo_runner import DemoRunner, HttpFactory, LlmFactory
from server.services.deposit_manager import DepositManager
from server.services.explainer import DepositExplainer
from server.services.payment_service import (
    MockPaymentProvider,
    PaymentProvider,
    PayPalSandboxProvider,
)
from server.services.paypal_assistant import PayPalAssistant
from server.services.paypal_mcp import PayPalMCPClient
from server.services.policy_assistant import PolicyAssistant
from server.services.origin_proxy import OriginProxy
from server.services.payment_methods import PaymentMethodStore
from server.services.playground import PlaygroundRunner
from server.services.policy_engine import PolicyEngine
from server.services.pricing import PricingCatalog
from server.services.rate_limiter import SlidingWindowRateLimiter
from server.services.runtime_config import (
    RuntimeConfigStore,
    StoredRuntimeConfig,
    bump_policy_version,
    effective_origin_url,
    merge_runtime_config,
    origin_status,
    validate_demo_rate_limit,
    validate_origin_url,
    validate_paths,
    validate_price,
)


class AppContainer:
    """Holds initialized services for a FastAPI application instance."""

    def __init__(
        self,
        settings: Settings,
        payment_provider: Optional[PaymentProvider] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        ai_generator: Optional[TextGenerator] = None,
        paypal_mcp: Optional[PayPalMCPClient] = None,
        demo_http_factory: Optional[HttpFactory] = None,
        demo_llm_factory: Optional[LlmFactory] = None,
        origin_proxy: Optional[OriginProxy] = None,
    ) -> None:
        self.settings = settings
        # Live owner-editable configuration, layered over env defaults.
        self.runtime_store = RuntimeConfigStore(settings.gateway_config_path)
        self.runtime_config = self.runtime_store.load()
        self.content_service = ContentService(settings, self.runtime_config)
        self.site_config = self.content_service.site_config
        # Reverse-proxy mode: enforce the origin website's own robots.txt as well.
        effective_origin = effective_origin_url(settings, self.runtime_config)
        self.origin_proxy = origin_proxy or (
            OriginProxy(effective_origin, settings.origin_timeout_seconds) if effective_origin else None
        )
        if self.origin_proxy is not None:
            self.site_config = merge_origin_rules(self.site_config, self.origin_proxy.import_robots())
        # Central tier pricing (data/site_config.json -> pricing_tiers), shared by the 402
        # challenge, the policy engine, the demo, and the dashboards.
        self.pricing = PricingCatalog(self.site_config)
        # A generic owner site's tier resources need not exist in SARA's bundled demo pages.
        if self.origin_proxy is None:
            self.pricing.validate_resources(self.content_service.allowed_page_paths)
        self.identity_service = IdentityService(self.site_config, self.pricing)
        self.audit_logger = AuditLogger(settings.audit_log_path)
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.policy_engine = PolicyEngine(
            site_config=self.site_config,
            rate_limiter=self.rate_limiter,
            known_allowed_pages=self.content_service.allowed_page_paths,
            pricing=self.pricing,
            default_allow=self.origin_proxy is not None,
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
            settlement_mode=SettlementMode(settings.deposit_settlement),
        )
        # Saved payment methods (PayPal vault tokens), kept next to the payments database.
        self.payment_methods = PaymentMethodStore(settings.payments_db_path.with_name("payment_methods_db.json"))
        self.deposit_manager.payment_methods = self.payment_methods
        # Agent commerce: pay-per-access items, PayPal subscriptions, and products.
        self.commerce = CommerceManager(
            db_path=settings.commerce_db_path,
            catalog=CommerceCatalog(settings.commerce_catalog_path, self.site_config.currency),
            payment_provider=self.payment_provider,
            audit_logger=self.audit_logger,
        )
        self.commerce.payment_methods = self.payment_methods
        self.session_manager = SessionManager(
            sessions_db_path=settings.sessions_db_path,
            site_config=self.site_config,
        )
        self.ai_generator: TextGenerator = ai_generator or build_text_generator(settings)
        self.explainer = DepositExplainer(self.ai_generator, self.site_config)
        self.policy_assistant = PolicyAssistant(self.ai_generator, self.site_config)
        self.paypal_mcp = paypal_mcp or PayPalMCPClient(settings)
        self.paypal_assistant = PayPalAssistant(self.ai_generator, self.paypal_mcp, self.deposit_manager, self.commerce)
        self.paypal_quota = HourlyQuota(settings.demo_max_questions_per_hour)
        self.config_quota = HourlyQuota(settings.config_max_writes_per_hour)
        # Verified agents: the site's directory of known keys, plus a key for this server's own
        # demo agents (generated at startup, never written to disk).
        self.agent_directory = AgentDirectory.load(settings.agent_directory_path)
        self.demo_agent_signer = WebBotAuth(Ed25519PrivateKey.generate())
        self.agent_directory.add("SARA demo agent", "Sartech Labs (demo)", self.demo_agent_signer.jwk)
        self.demo_runner = DemoRunner(
            max_runs_per_hour=settings.demo_max_runs_per_hour,
            max_deposit=self.pricing.max_price(),
            http_factory=demo_http_factory,
            llm_factory=demo_llm_factory,
        )
        self.demo_runner.agent_signer = self.demo_agent_signer
        self.playground_runner = PlaygroundRunner(
            max_runs_per_hour=settings.playground_max_runs_per_hour,
            ai_generator=self.ai_generator,
            max_actions=settings.playground_max_actions,
            agent_timeout_seconds=settings.playground_agent_timeout_seconds,
            agent_max_turns=settings.playground_agent_max_turns,
        )

    def load_persisted_state(self) -> None:
        """Load audit log, payment/challenge records, and session snapshots from disk."""
        self.audit_logger.load_from_disk()
        self.deposit_manager.load_from_disk()
        self.commerce.load_from_disk()
        self.session_manager.load_from_disk()

    # --- live runtime configuration -------------------------------------------------

    @property
    def config_public(self) -> bool:
        return bool(self.settings.demo_config_public and self.settings.sara_mode == "demo")

    @property
    def origin_allowlist(self) -> set[str]:
        return {
            host.strip().lower()
            for host in self.settings.demo_origin_allowlist.split(",")
            if host.strip()
        }

    def runtime_config_response(self) -> RuntimeConfigResponse:
        warnings: list[str] = []
        status = origin_status(self.origin_proxy)
        if self.settings.sara_mode == "gateway" and self.origin_proxy is None:
            warnings.append("Gateway mode has no target website URL; human traffic will return 404.")
        if self.origin_proxy is not None and status.error:
            warnings.append(f"Origin not reachable: {status.error}")
        return RuntimeConfigResponse(
            sara_mode=self.settings.sara_mode,
            origin_url=effective_origin_url(self.settings, self.runtime_config),
            origin_configured=self.origin_proxy is not None,
            origin_status=status,
            deposit_amount=self.site_config.deposit_amount,
            currency=self.site_config.currency,
            pricing_tiers=self.site_config.pricing_tiers,
            allowed_paths=list(self.site_config.allowed_paths),
            disallowed_paths=list(self.site_config.disallowed_paths),
            max_requests_per_session=self.site_config.max_requests_per_session,
            rate_limit_requests=self.site_config.rate_limit_requests,
            rate_limit_window_seconds=self.site_config.rate_limit_window_seconds,
            policy_version=self.site_config.policy_version,
            config_public=self.config_public,
            updated_at=self.runtime_config.updated_at,
            updated_by=self.runtime_config.updated_by,
            version=self.runtime_config.version,
            warnings=warnings,
        )

    async def apply_runtime_config(
        self,
        update: RuntimeConfigUpdate,
        *,
        actor: str,
        allow_private_origin: bool,
    ) -> RuntimeConfigResponse:
        """Validate, build, persist, and atomically swap the live gateway configuration."""
        # Validate editable values before touching the running gateway.
        if update.deposit_amount is not None:
            update.deposit_amount = validate_price(update.deposit_amount, field="deposit_amount")
        if update.tier_prices is not None:
            known = {tier.tier_id for tier in self.site_config.pricing_tiers}
            unknown = set(update.tier_prices) - known
            if unknown:
                raise ValueError(f"Unknown pricing tier(s): {', '.join(sorted(unknown))}")
            update.tier_prices = {
                tier_id: validate_price(price, field=f"tier {tier_id} price")
                for tier_id, price in update.tier_prices.items()
            }
        if update.allowed_paths is not None:
            update.allowed_paths = validate_paths(update.allowed_paths, field="allowed")
            update.allowed_paths = without_origin_paths(
                update.allowed_paths, self.origin_proxy.rules.allow if self.origin_proxy else []
            )
        if update.disallowed_paths is not None:
            update.disallowed_paths = validate_paths(
                update.disallowed_paths, field="disallowed", allow_root=False
            )
            update.disallowed_paths = without_origin_paths(
                update.disallowed_paths, self.origin_proxy.rules.disallow if self.origin_proxy else []
            )
        if update.origin_url is not None:
            previous_origin = effective_origin_url(self.settings, self.runtime_config)
            requested_origin = update.origin_url.strip().rstrip("/")
            if requested_origin == previous_origin.rstrip("/"):
                # A round-tripped value is not a change: keep the env/runtime default.
                update.origin_url = None
                update.model_fields_set.discard("origin_url")
            else:
                update.origin_url = validate_origin_url(
                    update.origin_url,
                    allow_private=allow_private_origin,
                    allowlist=self.origin_allowlist,
                    require_allowlist=not allow_private_origin,
                )

        previous = self.runtime_config
        merged = merge_runtime_config(previous, update, actor=actor)

        # Demo deployments are open to anonymous visitors, so cap live rate-limit edits.
        # Validate the effective values (a request may set only one of the two fields).
        if self.settings.sara_mode == "demo" and (
            "rate_limit_requests" in update.model_fields_set
            or "rate_limit_window_seconds" in update.model_fields_set
        ):
            requested_requests = (
                update.rate_limit_requests
                if update.rate_limit_requests is not None
                else self.site_config.rate_limit_requests
            )
            requested_window = (
                update.rate_limit_window_seconds
                if update.rate_limit_window_seconds is not None
                else self.site_config.rate_limit_window_seconds
            )
            validate_demo_rate_limit(requested_requests, requested_window)

        # Build the new policy. The origin's robots.txt is imported before the swap so a
        # bad target URL cannot leave the gateway half-configured.
        new_site_config = self.content_service.build_site_config(merged)
        new_origin_url = effective_origin_url(self.settings, merged)
        new_origin = OriginProxy(new_origin_url, self.settings.origin_timeout_seconds) if new_origin_url else None
        if new_origin is not None:
            await asyncio.to_thread(new_origin.import_robots)
            new_site_config = merge_origin_rules(new_site_config, new_origin.rules)

        # Bump the policy version when rules or limits change, so agents re-accept the policy.
        rules_changed = (
            new_site_config.allowed_paths != self.site_config.allowed_paths
            or new_site_config.disallowed_paths != self.site_config.disallowed_paths
            or new_site_config.max_requests_per_session != self.site_config.max_requests_per_session
            or new_site_config.rate_limit_requests != self.site_config.rate_limit_requests
            or new_site_config.rate_limit_window_seconds != self.site_config.rate_limit_window_seconds
            or new_origin_url != effective_origin_url(self.settings, previous)
        )
        if rules_changed and update.policy_version is None:
            merged.policy_version = bump_policy_version(self.site_config.policy_version)
            new_site_config = self.content_service.build_site_config(merged)
            if new_origin is not None:
                new_site_config = merge_origin_rules(new_site_config, new_origin.rules)

        new_pricing = PricingCatalog(new_site_config)
        if new_origin is None:
            new_pricing.validate_resources(self.content_service.allowed_page_paths)

        # Persist only after the whole candidate validates.
        self.runtime_store.save(merged)

        self.runtime_config = merged
        self.content_service.apply_runtime(merged, new_site_config)
        self.origin_proxy = new_origin
        self.site_config = new_site_config
        self.pricing = new_pricing
        self.identity_service = IdentityService(self.site_config, self.pricing)
        self.policy_engine = PolicyEngine(
            site_config=self.site_config,
            rate_limiter=self.rate_limiter,
            known_allowed_pages=self.content_service.allowed_page_paths,
            pricing=self.pricing,
            default_allow=self.origin_proxy is not None,
        )
        self.deposit_manager.set_site_config(self.site_config)
        self.session_manager.set_site_config(self.site_config)
        self.explainer.set_site_config(self.site_config)
        self.policy_assistant.set_site_config(self.site_config)
        self.demo_runner.set_max_deposit(self.pricing.max_price())

        await self.audit_logger.log_event(
            event_type="CONFIG_UPDATED",
            allowed=True,
            reason_code="CONFIG_APPLIED",
            policy_version=self.site_config.policy_version,
            details={
                "actor": actor,
                "version": merged.version,
                "origin_url": effective_origin_url(self.settings, merged),
                "rules_changed": rules_changed,
                "policy_version": self.site_config.policy_version,
                "deposit_amount": self.site_config.deposit_amount,
                "tier_prices": {t.tier_id: t.price for t in self.site_config.pricing_tiers},
                "max_requests_per_session": self.site_config.max_requests_per_session,
                "rate_limit_requests": self.site_config.rate_limit_requests,
                "rate_limit_window_seconds": self.site_config.rate_limit_window_seconds,
            },
        )
        return self.runtime_config_response()


def without_origin_paths(paths: list[str], origin_paths: list[str]) -> list[str]:
    """Drop imported origin rules so only owner-supplied paths are persisted."""
    imported = {(path.rstrip("/") or "/") for path in origin_paths}
    return [path for path in paths if (path.rstrip("/") or "/") not in imported]


def merge_origin_rules(site_config, rules):  # type: ignore[no-untyped-def]
    """Add the origin's robots.txt rules to SARA's own, and honour its Crawl-delay."""
    def merged(base: list[str], extra: list[str]) -> list[str]:
        return list(dict.fromkeys([*base, *extra]))

    update: dict = {
        "disallowed_paths": merged(site_config.disallowed_paths, rules.disallow),
        "allowed_paths": merged(site_config.allowed_paths, rules.allow),
    }
    delay = rules.crawl_delay_seconds
    if delay and site_config.rate_limit_window_seconds / site_config.rate_limit_requests < delay:
        update["rate_limit_window_seconds"] = float(site_config.rate_limit_requests * delay)
    return site_config.model_copy(update=update)


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


def demo_access(container: AppContainer, x_admin_token: Optional[str]) -> str:
    """'admin' with a valid admin token, 'public' when DEMO_PUBLIC=true, else 'none'."""
    if is_valid_admin_token(container, x_admin_token):
        return "admin"
    if x_admin_token is None and container.settings.demo_public:
        return "public"
    return "none"


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

