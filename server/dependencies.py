"""Application service container and FastAPI dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.web_bot_auth import WebBotAuth
from server.config import Settings
from server.core.agent_identity import AgentDirectory
from server.core.identity import IdentityService
from server.core.owner_auth import OwnerAuth
from server.core.site_context import set_current_site
from server.services.paypal_login import PayPalLogin
from server.core.quotas import HourlyQuota
from server.core.security import constant_time_equals, utc_now_iso
from server.core.sessions import SessionManager
from server.schemas import (
    PaymentMode,
    RuntimeConfigResponse,
    RuntimeConfigUpdate,
    SessionRecord,
    SettlementMode,
    SiteConfig,
    SiteCreateRequest,
    SiteListResponse,
    SiteMetaUpdate,
    SiteSummary,
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
    StoredRuntimeConfig,
    bump_policy_version,
    effective_origin_url,
    merge_runtime_config,
    origin_status,
    validate_origin_url,
    validate_paths,
    validate_price,
)
from server.services.site_registry import (
    GatewaySite,
    SiteRegistryData,
    SiteRegistryStore,
    host_of,
    slugify_site_id,
)


class SiteNotFoundError(KeyError):
    """The requested site id is not in the registry."""


@dataclass
class SiteRuntime:
    """Services built for one configured website."""

    site: GatewaySite
    content_service: ContentService
    site_config: SiteConfig
    pricing: PricingCatalog
    policy_engine: PolicyEngine
    identity_service: IdentityService
    origin_proxy: Optional[OriginProxy]


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
        self.owner_auth = OwnerAuth(settings)
        self.paypal_login = PayPalLogin(settings)
        # Multi-site registry, stored in the same file as the old flat config.
        self.registry_store = SiteRegistryStore(
            settings.gateway_config_path,
            default_origin_url=settings.origin_url,
        )
        # The engine always has a site to run on. Before the owner adds a website (with
        # SEED_DEFAULT_SITE=false) it runs on this placeholder, which is never listed.
        self._fallback_site = self.registry_store.default_registry().sites[0]
        if settings.seed_default_site or settings.gateway_config_path.exists():
            self.registry = self.registry_store.load()
        else:
            self.registry = SiteRegistryData(active_site_id=None, sites=[])
        if not self.registry.sites and settings.seed_default_site:
            self.registry = self.registry_store.default_registry()
        if self.registry.active_site_id not in {site.site_id for site in self.registry.sites}:
            self.registry.active_site_id = self.registry.sites[0].site_id if self.registry.sites else None
        self._runtimes: dict[str, SiteRuntime] = {}
        # Tests and embedded deployments can inject a proxy; bind it to the startup site so
        # later host/site routing still builds independent proxies for every other site.
        self._origin_proxy_override = origin_proxy
        self._origin_proxy_override_site_id = self.registry.active_site_id if origin_proxy else None
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        active_runtime = self._build_site_runtime(self._active_site)
        self._runtimes[active_runtime.site.site_id] = active_runtime
        active_site_config = active_runtime.site_config

        self.audit_logger = AuditLogger(settings.audit_log_path)
        if payment_provider is not None:
            self.payment_provider = payment_provider
        elif settings.payment_mode == PaymentMode.PAYPAL_SANDBOX.value:
            self.payment_provider = PayPalSandboxProvider(settings)
        else:
            self.payment_provider = MockPaymentProvider()

        self.deposit_manager = DepositManager(
            payments_db_path=settings.payments_db_path,
            site_config=active_site_config,
            payment_provider=self.payment_provider,
            audit_logger=self.audit_logger,
            settlement_mode=SettlementMode(settings.deposit_settlement),
        )
        self.deposit_manager.set_site_config_resolver(self._site_config_for_id)
        # Saved payment methods (PayPal vault tokens), kept next to the payments database.
        self.payment_methods = PaymentMethodStore(settings.payments_db_path.with_name("payment_methods_db.json"))
        self.deposit_manager.payment_methods = self.payment_methods
        # Agent commerce: pay-per-access items, PayPal subscriptions, and products.
        self.commerce = CommerceManager(
            db_path=settings.commerce_db_path,
            catalog=CommerceCatalog(settings.commerce_catalog_path, active_site_config.currency),
            payment_provider=self.payment_provider,
            audit_logger=self.audit_logger,
        )
        self.commerce.payment_methods = self.payment_methods
        self.session_manager = SessionManager(
            sessions_db_path=settings.sessions_db_path,
            site_config=active_site_config,
        )
        self.ai_generator: TextGenerator = ai_generator or build_text_generator(settings)
        self.explainer = DepositExplainer(self.ai_generator, active_site_config)
        self.policy_assistant = PolicyAssistant(self.ai_generator, active_site_config)
        self.paypal_mcp = paypal_mcp or PayPalMCPClient(settings)
        self.paypal_assistant = PayPalAssistant(self.ai_generator, self.paypal_mcp, self.deposit_manager, self.commerce)
        self.paypal_quota = HourlyQuota(settings.demo_max_questions_per_hour)
        # Verified agents: the site's directory of known keys, plus a key for this server's own
        # demo agents (generated at startup, never written to disk).
        self.agent_directory = AgentDirectory.load(settings.agent_directory_path)
        self.demo_agent_signer = WebBotAuth(Ed25519PrivateKey.generate())
        self.agent_directory.add("SARA demo agent", "Sartech Labs (demo)", self.demo_agent_signer.jwk)
        self.demo_runner = DemoRunner(
            max_runs_per_hour=settings.demo_max_runs_per_hour,
            max_deposit=active_runtime.pricing.max_price(),
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

    # --- multi-site runtime ---------------------------------------------------------

    @property
    def _active_site(self) -> GatewaySite:
        active_id = self.registry.active_site_id
        for site in self.registry.sites:
            if site.site_id == active_id:
                return site
        return self.registry.sites[0] if self.registry.sites else self._fallback_site

    @property
    def active_site_id(self) -> Optional[str]:
        return self.registry.active_site_id

    @property
    def site_config(self) -> SiteConfig:
        return self._active_runtime.site_config

    @property
    def pricing(self) -> PricingCatalog:
        return self._active_runtime.pricing

    @property
    def origin_proxy(self) -> Optional[OriginProxy]:
        return self._active_runtime.origin_proxy

    @property
    def content_service(self) -> ContentService:
        return self._active_runtime.content_service

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._active_runtime.policy_engine

    @property
    def identity_service(self) -> IdentityService:
        return self._active_runtime.identity_service

    @property
    def runtime_config(self) -> StoredRuntimeConfig:
        return self._active_runtime.site.config

    @property
    def _active_runtime(self) -> SiteRuntime:
        return self._runtime_for_site(self._active_site)

    def _site_by_id(self, site_id: Optional[str]) -> Optional[GatewaySite]:
        if not site_id:
            return None
        return next((site for site in self.registry.sites if site.site_id == site_id), None)

    def _runtime_for_site(self, site: GatewaySite) -> SiteRuntime:
        runtime = self._runtimes.get(site.site_id)
        if runtime is None:
            runtime = self._build_site_runtime(site)
            self._runtimes[site.site_id] = runtime
        return runtime

    def _runtime_for_id(self, site_id: str) -> SiteRuntime:
        site = self._site_by_id(site_id)
        if site is None:
            raise SiteNotFoundError(site_id)
        return self._runtime_for_site(site)

    def _site_config_for_id(self, site_id: str) -> SiteConfig:
        """Resolver used by deposits/sessions so old and new records use their own site."""
        site = self._site_by_id(site_id)
        return self._runtime_for_site(site).site_config if site is not None else self.site_config

    @staticmethod
    def _host_matches(request_host: str, site_host: str) -> bool:
        requested = request_host.strip().lower().split(":", 1)[0]
        return bool(site_host) and requested == site_host.strip().lower().split(":", 1)[0]

    def resolve_site(self, site_id: Optional[str] = None, host: Optional[str] = None) -> SiteRuntime:
        """Resolve a request's site: explicit id, then Host, then the active site.

        The resolved site is recorded for this request, so its audit events name it."""
        runtime = self._resolve_site_runtime(site_id=site_id, host=host)
        set_current_site(runtime.site.site_id)
        return runtime

    def _resolve_site_runtime(self, site_id: Optional[str] = None, host: Optional[str] = None) -> SiteRuntime:
        if site_id:
            return self._runtime_for_id(site_id)
        if host:
            for site in self.registry.sites:
                origin = effective_origin_url(self.settings, site.config)
                site_host = host_of(origin)
                if self._host_matches(host, site_host):
                    return self._runtime_for_site(site)
        return self._active_runtime

    def _build_site_runtime(
        self,
        site: GatewaySite,
        *,
        existing: Optional[SiteRuntime] = None,
    ) -> SiteRuntime:
        content = ContentService(
            self.settings,
            site.config,
            site_id=site.site_id,
            site_name=site.name,
        )
        site_config = content.site_config
        origin_url = effective_origin_url(self.settings, site.config)
        origin_proxy: Optional[OriginProxy] = None
        reused_proxy = False
        if existing is not None and existing.origin_proxy is not None:
            existing_url = str(existing.origin_proxy.base_url).rstrip("/")
            if (
                origin_url.rstrip("/") == existing_url
                or site.site_id == self._origin_proxy_override_site_id
            ):
                origin_proxy = existing.origin_proxy
                reused_proxy = True
        if origin_proxy is None and (
            self._origin_proxy_override is not None
            and site.site_id == self._origin_proxy_override_site_id
        ):
            origin_proxy = self._origin_proxy_override
        if origin_proxy is None and origin_url:
            origin_proxy = OriginProxy(origin_url, self.settings.origin_timeout_seconds)
        if origin_proxy is not None:
            if not reused_proxy:
                origin_proxy.import_robots()
            site_config = merge_origin_rules(site_config, origin_proxy.rules)
        content.apply_runtime(site.config, site_config)
        pricing = PricingCatalog(site_config)
        if origin_proxy is None:
            # A generic owner site's tier resources need not exist in SARA's demo pages.
            pricing.validate_resources(content.allowed_page_paths)
        return SiteRuntime(
            site=site,
            content_service=content,
            site_config=site_config,
            pricing=pricing,
            policy_engine=PolicyEngine(
                site_config=site_config,
                rate_limiter=self.rate_limiter,
                known_allowed_pages=content.allowed_page_paths,
                pricing=pricing,
                default_allow=origin_proxy is not None,
            ),
            identity_service=IdentityService(site_config, pricing),
            origin_proxy=origin_proxy,
        )

    def _sync_active_services(self) -> None:
        runtime = self._active_runtime
        self.deposit_manager.set_site_config(runtime.site_config)
        self.session_manager.set_site_config(runtime.site_config)
        self.explainer.set_site_config(runtime.site_config)
        self.policy_assistant.set_site_config(runtime.site_config)
        self.demo_runner.set_max_deposit(runtime.pricing.max_price())

    def _site_summary(self, runtime: SiteRuntime) -> SiteSummary:
        site = runtime.site
        config = site.config
        origin_url = effective_origin_url(self.settings, config)
        return SiteSummary(
            site_id=site.site_id,
            name=site.name,
            host=host_of(origin_url),
            origin_url=origin_url,
            status=site.status,
            active=site.site_id == self.active_site_id,
            origin_configured=runtime.origin_proxy is not None,
            deposit_amount=runtime.site_config.deposit_amount,
            currency=runtime.site_config.currency,
            allowed_paths=list(runtime.site_config.allowed_paths),
            disallowed_paths=list(runtime.site_config.disallowed_paths),
            rate_limit_requests=runtime.site_config.rate_limit_requests,
            rate_limit_window_seconds=runtime.site_config.rate_limit_window_seconds,
            max_requests_per_session=runtime.site_config.max_requests_per_session,
            policy_version=runtime.site_config.policy_version,
            version=config.version,
            updated_at=config.updated_at,
            origin_status=origin_status(runtime.origin_proxy),
        )

    def list_site_summaries(self) -> SiteListResponse:
        return SiteListResponse(
            active_site_id=self.active_site_id,
            config_public=self.config_public,
            sites=[self._site_summary(self._runtime_for_site(site)) for site in self.registry.sites],
        )

    def _unique_site_id(self, origin_url: str) -> str:
        base = slugify_site_id(origin_url)
        candidate = base
        suffix = 2
        used = {site.site_id for site in self.registry.sites}
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    async def add_site(
        self,
        request: SiteCreateRequest,
        *,
        actor: str,
        allow_private_origin: bool,
    ) -> RuntimeConfigResponse:
        origin_url = validate_origin_url(
            request.origin_url,
            allow_private=allow_private_origin,
            allowlist=self.origin_allowlist,
            require_allowlist=False,
        )
        if not origin_url:
            raise ValueError("Origin URL is required.")
        # Gateway traffic is routed by Host, so two sites for the same host would be ambiguous.
        new_host = host_of(origin_url)
        if new_host and any(host_of(site.config.origin_url) == new_host for site in self.registry.sites):
            raise ValueError(f"{new_host} is already on the gateway.")
        deposit_amount = (
            validate_price(request.deposit_amount, field="deposit_amount")
            if request.deposit_amount is not None
            else f"{self.settings.deposit_amount:.2f}"
        )
        tiers = []
        for tier in request.pricing_tiers or []:
            tiers.append(
                tier.model_copy(update={"price": validate_price(tier.price, field=f"tier {tier.tier_id} price")})
            )
        now = utc_now_iso()
        config = StoredRuntimeConfig(
            origin_url=origin_url,
            deposit_amount=deposit_amount,
            allowed_paths=validate_paths(request.allowed_paths or [], field="allowed"),
            disallowed_paths=validate_paths(
                request.disallowed_paths or [], field="disallowed", allow_root=False
            ),
            rate_limit_requests=(
                request.rate_limit_requests
                if request.rate_limit_requests is not None
                else self.settings.rate_limit_requests
            ),
            rate_limit_window_seconds=(
                request.rate_limit_window_seconds
                if request.rate_limit_window_seconds is not None
                else self.settings.rate_limit_window_seconds
            ),
            max_requests_per_session=(
                request.max_requests_per_session
                if request.max_requests_per_session is not None
                else self.settings.max_requests_per_session
            ),
            policy_version="1.0",
            pricing_tiers=tiers,
            version=0,
            updated_at=now,
            updated_by=actor[:120],
        )
        name = (request.name or "").strip() or host_of(origin_url) or "SARA site"
        site = GatewaySite(
            site_id=self._unique_site_id(origin_url),
            name=name,
            status="live",
            config=config,
            created_at=now,
            updated_at=now,
        )
        # Build (and import robots.txt) before persisting so a broken candidate cannot
        # leave the registry half-updated. The origin fetch is blocking, so keep it off
        # the event loop: an unreachable site must not freeze the console.
        runtime = await asyncio.to_thread(self._build_site_runtime, site)
        self.registry.sites.append(site)
        self._runtimes[site.site_id] = runtime
        if request.set_active or not self.registry.active_site_id:
            self.registry.active_site_id = site.site_id
            self._sync_active_services()
        self.registry_store.save(self.registry)
        return self.runtime_config_response(site.site_id)

    def update_site_meta(
        self,
        site_id: str,
        *,
        name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> SiteSummary:
        site = self._site_by_id(site_id)
        if site is None:
            raise SiteNotFoundError(site_id)
        changed = False
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise ValueError("Site name cannot be empty.")
            site.name = cleaned[:128]
            changed = True
        if status is not None:
            if status not in ("live", "paused"):
                raise ValueError("Site status must be 'live' or 'paused'.")
            site.status = status  # type: ignore[assignment]
            changed = True
        if changed:
            site.updated_at = utc_now_iso()
            existing = self._runtimes.get(site.site_id)
            runtime = self._build_site_runtime(site, existing=existing)
            self._runtimes[site.site_id] = runtime
            if site.site_id == self.active_site_id:
                self._sync_active_services()
            self.registry_store.save(self.registry)
        return self._site_summary(self._runtime_for_site(site))

    def delete_site(self, site_id: str) -> None:
        site = self._site_by_id(site_id)
        if site is None:
            raise SiteNotFoundError(site_id)
        if len(self.registry.sites) <= 1 and self.settings.seed_default_site:
            raise ValueError("At least one site must remain configured.")
        was_active = site.site_id == self.active_site_id
        self.registry.sites = [item for item in self.registry.sites if item.site_id != site_id]
        self._runtimes.pop(site_id, None)
        if was_active:
            self.registry.active_site_id = self.registry.sites[0].site_id if self.registry.sites else None
            self._sync_active_services()
        self.registry_store.save(self.registry)

    def set_active_site(self, site_id: str) -> SiteListResponse:
        site = self._site_by_id(site_id)
        if site is None:
            raise SiteNotFoundError(site_id)
        self.registry.active_site_id = site_id
        self._sync_active_services()
        self.registry_store.save(self.registry)
        return self.list_site_summaries()

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

    def runtime_config_response(self, site_id: Optional[str] = None) -> RuntimeConfigResponse:
        runtime = self.resolve_site(site_id=site_id) if site_id else self._active_runtime
        site = runtime.site
        config = site.config
        origin_url = effective_origin_url(self.settings, config)
        warnings: list[str] = []
        status = origin_status(runtime.origin_proxy)
        if self.settings.sara_mode == "gateway" and runtime.origin_proxy is None:
            warnings.append("Gateway mode has no target website URL; human traffic will return 404.")
        if runtime.origin_proxy is not None and status.error:
            warnings.append(f"Origin not reachable: {status.error}")
        return RuntimeConfigResponse(
            site_id=site.site_id,
            site_name=site.name,
            status=site.status,
            active_site_id=self.active_site_id,
            host=host_of(origin_url),
            sara_mode=self.settings.sara_mode,
            origin_url=origin_url,
            origin_configured=runtime.origin_proxy is not None,
            origin_status=status,
            deposit_amount=runtime.site_config.deposit_amount,
            currency=runtime.site_config.currency,
            pricing_tiers=runtime.site_config.pricing_tiers,
            allowed_paths=list(runtime.site_config.allowed_paths),
            disallowed_paths=list(runtime.site_config.disallowed_paths),
            max_requests_per_session=runtime.site_config.max_requests_per_session,
            rate_limit_requests=runtime.site_config.rate_limit_requests,
            rate_limit_window_seconds=runtime.site_config.rate_limit_window_seconds,
            policy_version=runtime.site_config.policy_version,
            config_public=self.config_public,
            updated_at=config.updated_at,
            updated_by=config.updated_by,
            version=config.version,
            warnings=warnings,
        )

    async def apply_runtime_config(
        self,
        update: RuntimeConfigUpdate,
        *,
        actor: str,
        allow_private_origin: bool,
        site_id: Optional[str] = None,
    ) -> RuntimeConfigResponse:
        """Validate, build, persist, and atomically swap one site's live configuration."""
        runtime = self.resolve_site(site_id=site_id) if site_id else self._active_runtime
        site = runtime.site
        content = runtime.content_service
        current_site_config = runtime.site_config
        current_proxy = runtime.origin_proxy
        previous = site.config

        # Validate editable values before touching the running gateway.
        if update.deposit_amount is not None:
            update.deposit_amount = validate_price(update.deposit_amount, field="deposit_amount")
        if update.pricing_tiers is not None:
            update.pricing_tiers = [
                tier.model_copy(
                    update={"price": validate_price(tier.price, field=f"tier {tier.tier_id} price")}
                )
                for tier in update.pricing_tiers
            ]
        if update.tier_prices is not None:
            tier_source = (
                update.pricing_tiers
                if update.pricing_tiers is not None
                else current_site_config.pricing_tiers
            )
            known = {tier.tier_id for tier in tier_source}
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
                update.allowed_paths, current_proxy.rules.allow if current_proxy else []
            )
        if update.disallowed_paths is not None:
            update.disallowed_paths = validate_paths(
                update.disallowed_paths, field="disallowed", allow_root=False
            )
            update.disallowed_paths = without_origin_paths(
                update.disallowed_paths, current_proxy.rules.disallow if current_proxy else []
            )
        previous_origin = effective_origin_url(self.settings, previous)
        if update.origin_url is not None:
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
                    require_allowlist=False,
                )

        merged = merge_runtime_config(previous, update, actor=actor)

        # Build the new policy. The origin's robots.txt is imported before the swap so a
        # bad target URL cannot leave the gateway half-configured.
        new_site_config = content.build_site_config(merged)
        new_origin_url = effective_origin_url(self.settings, merged)
        new_origin = current_proxy
        if (
            new_origin is None
            or str(new_origin.base_url).rstrip("/") != new_origin_url.rstrip("/")
        ):
            new_origin = (
                OriginProxy(new_origin_url, self.settings.origin_timeout_seconds)
                if new_origin_url
                else None
            )
        if new_origin is not None:
            await asyncio.to_thread(new_origin.import_robots)
            new_site_config = merge_origin_rules(new_site_config, new_origin.rules)

        # Bump the policy version when rules or limits change, so agents re-accept the policy.
        rules_changed = (
            new_site_config.allowed_paths != current_site_config.allowed_paths
            or new_site_config.disallowed_paths != current_site_config.disallowed_paths
            or new_site_config.max_requests_per_session != current_site_config.max_requests_per_session
            or new_site_config.rate_limit_requests != current_site_config.rate_limit_requests
            or new_site_config.rate_limit_window_seconds != current_site_config.rate_limit_window_seconds
            or new_origin_url != previous_origin
        )
        if rules_changed and update.policy_version is None:
            merged.policy_version = bump_policy_version(current_site_config.policy_version)
            new_site_config = content.build_site_config(merged)
            if new_origin is not None:
                new_site_config = merge_origin_rules(new_site_config, new_origin.rules)

        new_pricing = PricingCatalog(new_site_config)
        if new_origin is None:
            new_pricing.validate_resources(content.allowed_page_paths)

        # Persist only after the whole candidate validates.
        site.config = merged
        site.updated_at = utc_now_iso()
        self.registry_store.save(self.registry)

        content.apply_runtime(merged, new_site_config)
        new_runtime = SiteRuntime(
            site=site,
            content_service=content,
            site_config=new_site_config,
            pricing=new_pricing,
            policy_engine=PolicyEngine(
                site_config=new_site_config,
                rate_limiter=self.rate_limiter,
                known_allowed_pages=content.allowed_page_paths,
                pricing=new_pricing,
                default_allow=new_origin is not None,
            ),
            identity_service=IdentityService(new_site_config, new_pricing),
            origin_proxy=new_origin,
        )
        self._runtimes[site.site_id] = new_runtime
        if site.site_id == self.active_site_id:
            self._sync_active_services()

        await self.audit_logger.log_event(
            event_type="CONFIG_UPDATED",
            allowed=True,
            reason_code="CONFIG_APPLIED",
            policy_version=new_site_config.policy_version,
            details={
                "actor": actor,
                "site_id": site.site_id,
                "version": merged.version,
                "origin_url": new_origin_url,
                "rules_changed": rules_changed,
                "policy_version": new_site_config.policy_version,
                "deposit_amount": new_site_config.deposit_amount,
                "tier_prices": {t.tier_id: t.price for t in new_site_config.pricing_tiers},
                "max_requests_per_session": new_site_config.max_requests_per_session,
                "rate_limit_requests": new_site_config.rate_limit_requests,
                "rate_limit_window_seconds": new_site_config.rate_limit_window_seconds,
            },
        )
        return self.runtime_config_response(site.site_id)


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
    # Crawl-delay is a minimum gap between requests, not a rate window. Rewriting the
    # window here produced arbitrary values such as 330s in the console.
    update["crawl_delay_seconds"] = rules.crawl_delay_seconds
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
    """'admin' with a valid admin token, 'public' on a public demo, else 'none'."""
    if is_valid_admin_token(container, x_admin_token):
        return "admin"
    # A public-demo console may run the playground/agent even when the older
    # DEMO_PUBLIC flag is off: DEMO_CONFIG_PUBLIC already opens the same console.
    if x_admin_token is None and (container.settings.demo_public or container.config_public):
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

