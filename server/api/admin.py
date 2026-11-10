"""Owner console administration: live target URL, deposits, and crawling rules."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from server.dependencies import AppContainer, get_container, is_valid_admin_token
from server.schemas import OriginStatus, OriginTestRequest, RuntimeConfigResponse, RuntimeConfigUpdate
from server.services.runtime_config import OriginNotAllowedError, check_origin, validate_origin_url

router = APIRouter(prefix="/admin", tags=["Admin configuration"])


def _config_access(
    container: AppContainer, x_admin_token: Optional[str], request: Request
) -> str:
    """Return 'admin' or 'public'; reject when neither applies."""
    if is_valid_admin_token(container, x_admin_token):
        return "admin"
    if x_admin_token is None and container.config_public:
        return "public"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "config_authorization_required",
            "message": (
                "A valid X-Admin-Token is required. Demo deployments may enable "
                "DEMO_CONFIG_PUBLIC=true for public, rate-limited configuration."
            ),
        },
    )


def _client_actor(request: Request, access: str) -> str:
    if access == "admin":
        return "admin"
    host = request.client.host if request.client else "unknown"
    return f"public:{host}"


@router.get(
    "/config",
    response_model=RuntimeConfigResponse,
    summary="Read the live gateway configuration (target URL, deposits, rules)",
)
async def get_config(
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> RuntimeConfigResponse:
    _config_access(container, x_admin_token, request)
    return container.runtime_config_response()


@router.put(
    "/config",
    response_model=RuntimeConfigResponse,
    summary="Apply target URL, tier deposits, and crawling rules live",
)
async def update_config(
    payload: RuntimeConfigUpdate,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> RuntimeConfigResponse:
    access = _config_access(container, x_admin_token, request)
    if access == "public" and not container.config_quota.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "config_rate_limited",
                "message": "Too many public configuration changes. Try again later.",
            },
        )
    try:
        return await container.apply_runtime_config(
            payload,
            actor=_client_actor(request, access),
            allow_private_origin=access == "admin",
        )
    except OriginNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "origin_not_allowed", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_configuration", "message": str(exc)},
        ) from exc


@router.post(
    "/config/test-origin",
    response_model=OriginStatus,
    summary="Check that the target website is reachable and read its robots.txt",
)
async def test_origin(
    payload: OriginTestRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> OriginStatus:
    access = _config_access(container, x_admin_token, request)
    try:
        origin_url = validate_origin_url(
            payload.origin_url,
            allow_private=access == "admin",
            allowlist=container.origin_allowlist,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_origin_url", "message": str(exc)},
        ) from exc
    if not origin_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_origin_url", "message": "Origin URL is required."},
        )
    return await check_origin(origin_url, container.settings.origin_timeout_seconds)


@router.post(
    "/config/sync-robots",
    response_model=RuntimeConfigResponse,
    summary="Re-import the target website's robots.txt and apply it live",
)
async def sync_robots(
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> RuntimeConfigResponse:
    access = _config_access(container, x_admin_token, request)
    if access != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "admin_required",
                "message": "Sync robots.txt requires the admin token.",
            },
        )
    try:
        return await container.apply_runtime_config(
            RuntimeConfigUpdate(),
            actor=_client_actor(request, access),
            allow_private_origin=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_configuration", "message": str(exc)},
        ) from exc
