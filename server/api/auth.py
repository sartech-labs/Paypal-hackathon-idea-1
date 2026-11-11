"""Owner sign-in: the home page, sign-in and sign-out, and who is signed in."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from server.core.owner_auth import COOKIE_NAME, OwnerAuth, read_cookie
from server.dependencies import AppContainer, get_container

router = APIRouter(tags=["owner sign-in"])

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
HOME_PAGE = STATIC_DIR / "home.html"


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def owner_auth(container: AppContainer) -> OwnerAuth:
    return container.owner_auth


def signed_in_owner(request: Request, container: AppContainer) -> Optional[dict]:
    auth = owner_auth(container)
    return auth.verify(read_cookie(request.headers.get("cookie"), COOKIE_NAME))


def console_locked(request: Request, container: AppContainer) -> bool:
    """True when sign-in is on and this browser has no valid owner session."""
    return owner_auth(container).enabled and signed_in_owner(request, container) is None


def home_page() -> FileResponse:
    return FileResponse(HOME_PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})


def _secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def _set_cookie(response: Response, request: Request, auth: OwnerAuth, value: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=auth.max_age(),
        httponly=True,
        secure=_secure(request),
        samesite="lax",
        path="/",
    )


@router.get("/signin", include_in_schema=False)
async def signin_page(request: Request, container: AppContainer = Depends(get_container)) -> Response:
    if not console_locked(request, container):
        return RedirectResponse("/console", status_code=status.HTTP_303_SEE_OTHER)
    return home_page()


@router.get("/auth/me", summary="Who is signed in to the console")
async def auth_me(request: Request, container: AppContainer = Depends(get_container)) -> dict:
    auth = owner_auth(container)
    session = signed_in_owner(request, container)
    return {
        "login_enabled": auth.enabled,
        "signed_in": bool(session) or not auth.enabled,
        "email": (session or {}).get("email"),
        "sara_mode": container.settings.sara_mode,
        "payment_mode": container.payment_provider.mode.value,
    }


@router.post("/auth/login", summary="Sign in as the website owner")
async def auth_login(
    payload: LoginRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Response:
    auth = owner_auth(container)
    client = request.client.host if request.client else "unknown"
    if not auth.enabled:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "password_login_disabled", "message": "Password sign-in is not set up on this gateway."},
        )
    if not auth.allow_attempt(client):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "too_many_attempts", "message": "Too many sign-in attempts. Try again in 15 minutes."},
        )
    if not auth.check_password(payload.email, payload.password):
        auth.record_failure(client)
        await asyncio.sleep(0.4)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_credentials", "message": "That email and password do not match."},
        )
    response = JSONResponse({"signed_in": True, "email": container.settings.owner_email.strip()})
    _set_cookie(response, request, auth, auth.issue(container.settings.owner_email.strip()))
    return response


@router.post("/auth/logout", summary="Sign out of the console")
async def auth_logout() -> Response:
    response = JSONResponse({"signed_in": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
