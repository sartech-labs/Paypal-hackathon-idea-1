"""Owner console administration: live target URL, deposits, and crawling rules."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from server.core.console_auth import console_auth_enabled
from server.dependencies import AppContainer, get_container, is_console_admin, is_valid_admin_token
from server.schemas import (
    OriginStatus,
    OriginTestRequest,
    RuntimeConfigResponse,
    RuntimeConfigUpdate,
    SiteCreateRequest,
    SiteListResponse,
    SiteMetaUpdate,
    SiteSummary,
)
from server.services.runtime_config import OriginNotAllowedError, check_origin, validate_origin_url

router = APIRouter(prefix="/admin", tags=["Admin configuration"])


def _config_access(
    container: AppContainer, x_admin_token: Optional[str], request: Request
) -> str:
    """Return 'admin' or 'public'; reject when neither applies."""
    if is_valid_admin_token(container, x_admin_token):
        return "admin"
    if is_console_admin(request, container):
        return "admin"
    if (
        x_admin_token is None
        and not console_auth_enabled(container.settings)
        and container.config_public
    ):
        return "public"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "config_authorization_required",
            "message": (
                "A valid X-Admin-Token is required. Demo deployments may enable "
                "DEMO_CONFIG_PUBLIC=true for public configuration."
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
    site_id: Optional[str] = Query(default=None),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
    x_sara_site: Optional[str] = Header(default=None, alias="X-SARA-Site"),
) -> RuntimeConfigResponse:
    _config_access(container, x_admin_token, request)
    try:
        return container.runtime_config_response(site_id=site_id or x_sara_site)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc


@router.put(
    "/config",
    response_model=RuntimeConfigResponse,
    summary="Apply target URL, tier deposits, and crawling rules live",
)
async def update_config(
    payload: RuntimeConfigUpdate,
    request: Request,
    site_id: Optional[str] = Query(default=None),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
    x_sara_site: Optional[str] = Header(default=None, alias="X-SARA-Site"),
) -> RuntimeConfigResponse:
    access = _config_access(container, x_admin_token, request)
    try:
        return await container.apply_runtime_config(
            payload,
            actor=_client_actor(request, access),
            allow_private_origin=access == "admin",
            site_id=site_id or x_sara_site,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
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
            require_allowlist=False,
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
    site_id: Optional[str] = Query(default=None),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
    x_sara_site: Optional[str] = Header(default=None, alias="X-SARA-Site"),
) -> RuntimeConfigResponse:
    access = _config_access(container, x_admin_token, request)
    try:
        return await container.apply_runtime_config(
            RuntimeConfigUpdate(),
            actor=_client_actor(request, access),
            allow_private_origin=access == "admin",
            site_id=site_id or x_sara_site,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_configuration", "message": str(exc)},
        ) from exc


# --- Website management ------------------------------------------------------------------


@router.get(
    "/sites",
    response_model=SiteListResponse,
    summary="List configured websites and the active one",
)
async def list_sites(
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> SiteListResponse:
    _config_access(container, x_admin_token, request)
    return container.list_site_summaries()


@router.post(
    "/sites",
    response_model=RuntimeConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a website to the gateway",
)
async def create_site(
    payload: SiteCreateRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> RuntimeConfigResponse:
    access = _config_access(container, x_admin_token, request)
    try:
        return await container.add_site(
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
            detail={"error": "invalid_site", "message": str(exc)},
        ) from exc


@router.put(
    "/sites/{site_id}",
    response_model=SiteSummary,
    summary="Rename or pause/resume a website",
)
async def update_site(
    site_id: str,
    payload: SiteMetaUpdate,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> SiteSummary:
    _config_access(container, x_admin_token, request)
    try:
        return container.update_site_meta(
            site_id, name=payload.name, status=payload.status
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_site", "message": str(exc)},
        ) from exc


@router.delete(
    "/sites/{site_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a website (at least one remains)",
)
async def delete_site(
    site_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> Response:
    _config_access(container, x_admin_token, request)
    try:
        container.delete_site(site_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_site", "message": str(exc)},
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sites/{site_id}/activate",
    response_model=SiteListResponse,
    summary="Select the website the console edits and plays",
)
async def activate_site(
    site_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> SiteListResponse:
    _config_access(container, x_admin_token, request)
    try:
        return container.set_active_site(site_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
