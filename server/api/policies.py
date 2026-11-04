"""Public policy, robots.txt, health check, and admin policy-assistant endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import PlainTextResponse

from server.dependencies import AppContainer, get_container, is_valid_admin_token
from server.schemas import PolicyDraftRequest, PolicyDraftResponse, SiteConfig
from server.services.ai_client import AIRefusalError, AIServiceError, AIUnavailableError

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


@router.post(
    "/policies/draft",
    response_model=PolicyDraftResponse,
    summary="Draft a crawling policy from plain-English publisher terms with Claude (admin)",
)
async def draft_policy(
    body: PolicyDraftRequest,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PolicyDraftResponse:
    if not is_valid_admin_token(container, x_admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_admin_token",
                "message": "A valid configured X-Admin-Token is required.",
            },
        )
    try:
        return await container.policy_assistant.draft(body)
    except AIUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "ai_unavailable", "message": str(exc)},
        ) from exc
    except AIRefusalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "ai_refused", "message": str(exc)},
        ) from exc
    except AIServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "ai_error", "message": str(exc)},
        ) from exc


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
    summary="Service health, payment mode, and AI availability check",
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
        "deposit_settlement": container.settings.deposit_settlement,
        "ai": {
            "enabled": container.settings.ai_enabled,
            "available": container.ai_generator.is_available(),
            "model": container.ai_generator.model,
        },
    }
