"""Public policy, robots.txt, and health check endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from server.dependencies import AppContainer, get_container
from server.schemas import SiteConfig

router = APIRouter(tags=["Policy & Health"])


@router.get(
    "/policies",
    response_model=SiteConfig,
    summary="Return the current machine-readable website crawling and deposit policy",
)
async def get_policies(
    container: AppContainer = Depends(get_container),
) -> SiteConfig:
    return container.site_config


@router.get(
    "/robots.txt",
    response_class=PlainTextResponse,
    summary="Publish the website's effective robots.txt rules as plain text",
)
async def get_robots_txt(
    container: AppContainer = Depends(get_container),
) -> PlainTextResponse:
    content = container.content_service.render_robots_txt()
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@router.get(
    "/health",
    summary="Service health and runtime payment mode check",
)
async def health_check(
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "CrawlerTrust Gateway",
        "site_id": container.site_config.site_id,
        "policy_version": container.site_config.policy_version,
        "payment_mode": container.payment_provider.mode.value,
        "simulated_payments": container.payment_provider.mode.value == "mock",
    }

