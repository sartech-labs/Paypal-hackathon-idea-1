"""FastAPI application entrypoint for SARA."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.services.origin_proxy import OriginProxy
from server.api import admin, audit, auth, commerce, dashboard, webhooks, deposits, demo, gateway, paypal, playground, policies, sessions, site
from server.config import Settings, load_settings_from_env
from server.core.console_auth import COOKIE_NAME, console_auth_enabled
from server.dependencies import AppContainer, is_console_admin
from server.services.ai_client import TextGenerator
from server.services.demo_runner import HttpFactory, LlmFactory
from server.services.payment_service import PaymentProvider
from server.services.paypal_mcp import PayPalMCPClient
from server.services.rate_limiter import SlidingWindowRateLimiter
from server.services.storefront_mcp import Storefront

logger = logging.getLogger("sara")


def create_app(
    settings: Optional[Settings] = None,
    payment_provider: Optional[PaymentProvider] = None,
    rate_limiter: Optional[SlidingWindowRateLimiter] = None,
    ai_generator: Optional[TextGenerator] = None,
    paypal_mcp: Optional[PayPalMCPClient] = None,
    demo_http_factory: Optional[HttpFactory] = None,
    demo_llm_factory: Optional[LlmFactory] = None,
    origin_proxy: Optional[OriginProxy] = None,
) -> FastAPI:
    """Create and configure the SARA FastAPI application."""
    resolved_settings = settings or load_settings_from_env()
    logging.basicConfig(
        level=getattr(logging, resolved_settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    container = AppContainer(
        settings=resolved_settings,
        payment_provider=payment_provider,
        rate_limiter=rate_limiter,
        ai_generator=ai_generator,
        paypal_mcp=paypal_mcp,
        demo_http_factory=demo_http_factory,
        demo_llm_factory=demo_llm_factory,
        origin_proxy=origin_proxy,
    )
    container.load_persisted_state()

    storefront_holder: dict[str, Storefront] = {}

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        app_instance.state.container = container
        container.load_persisted_state()
        logger.info(
            "SARA Gateway started (site_id=%s, payment_mode=%s)",
            container.site_config.site_id,
            container.payment_provider.mode.value,
        )
        # The MCP storefront's session manager runs for the app's lifetime.
        async with storefront_holder["storefront"].server.session_manager.run():
            yield
        logger.info("SARA Gateway shutting down")

    app = FastAPI(
        title="SARA — Deposit-Based Accountability Gateway for AI Crawlers",
        version="0.1.0",
        description=(
            "Enforces website crawling policies, rate limits, and path restrictions using "
            "refundable deposits powered by PayPal Sandbox APIs (with deterministic mock mode)."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.state.container = container

    @app.exception_handler(HTTPException)
    async def custom_http_exception_handler(
        _request: Request, exc: HTTPException
    ) -> JSONResponse:
        if isinstance(exc.detail, dict):
            content = exc.detail
        else:
            content = {"error": "http_error", "message": str(exc.detail)}
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors = []
        for err in exc.errors():
            sanitized_errors.append(
                {
                    "loc": list(err.get("loc", [])),
                    "msg": str(err.get("msg", "")),
                    "type": str(err.get("type", "")),
                }
            )
        return JSONResponse(
            status_code=400,
            content={
                "error": "validation_error",
                "message": "Malformed request payload or parameters.",
                "details": sanitized_errors,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error("Unhandled server error: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "message": "An unexpected internal server error occurred.",
            },
        )

    @app.middleware("http")
    async def console_csrf_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Reject cross-origin state changes that ride on a console session cookie."""
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and COOKIE_NAME in request.cookies
            and is_console_admin(request, container)
        ):
            rejected = request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site"
            origin = request.headers.get("origin")
            if origin:
                origin_host = urlsplit(origin).netloc.lower()
                # An edge proxy may rewrite Host to the container's internal name, so the
                # configured public URL and X-Forwarded-Host are trusted as matches too.
                trusted_hosts = {
                    request.headers.get("host", "").lower(),
                    urlsplit(resolved_settings.app_public_url).netloc.lower(),
                    request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower(),
                }
                rejected = rejected or origin_host not in trusted_hosts
            if rejected:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "cross_origin_request",
                        "message": "Cross-origin console requests are not allowed.",
                    },
                )
        return await call_next(request)

    app.include_router(auth.session_router)
    # Sign-in routes exist only when a console password is configured; otherwise /login and
    # /logout keep falling through to the site edge exactly as before.
    if console_auth_enabled(resolved_settings):
        app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(gateway.router)
    app.include_router(deposits.router)
    app.include_router(sessions.router)
    app.include_router(policies.router)
    app.include_router(audit.router)
    app.include_router(dashboard.router)
    app.include_router(demo.router)
    app.include_router(playground.router)
    app.include_router(paypal.router)
    app.include_router(commerce.router)
    app.include_router(webhooks.router)
    # SARA's own assets live under /_sara so an origin website can keep /static.
    app.mount("/_sara/static", StaticFiles(directory=dashboard.STATIC_DIR), name="sara-static")

    # MCP storefront (Streamable HTTP) at /mcp: any MCP client can shop this site.
    storefront = Storefront(app)
    storefront_holder["storefront"] = storefront
    app.state.storefront = storefront
    app.mount("/mcp", storefront.asgi(), name="mcp")

    # Transparent site edge. Register last so every console/API/static route wins first.
    app.include_router(site.router)

    return app


app = create_app()

