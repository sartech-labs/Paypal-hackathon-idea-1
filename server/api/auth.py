"""Console sign-in endpoints and the signed session cookie."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from server.api.dashboard import NO_STORE, STATIC_DIR
from server.core.console_auth import (
    COOKIE_NAME,
    console_auth_enabled,
    sign_console_session,
)
from server.core.security import constant_time_equals
from server.dependencies import AppContainer, console_user, get_container

# Sign-in routes live under /_sara/ (next to /_sara/static) so enabling console auth
# never shadows a path on the proxied origin site: /login and /logout are common there.
router = APIRouter(tags=["Console auth"])
# /_sara/session stays available whether or not sign-in is enabled. The sign-in and
# sign-out routes are only registered when a password is configured, so an unauthenticated
# deployment keeps its previous behaviour exactly.
session_router = APIRouter(tags=["Console auth"])

LOGIN_RATE_LIMIT = 8
LOGIN_RATE_WINDOW_SECONDS = 300


def _safe_next(value: Optional[str]) -> str:
    """Return a same-site redirect target, falling back to the console."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/console"
    if any(ch in value for ch in ("\\", "\r", "\n", "\t")):
        return "/console"
    return value


def _cookie_secure(request: Request, container: AppContainer) -> bool:
    """Mark the cookie Secure on real HTTPS only; a Secure cookie is dropped on plain HTTP."""
    return (
        request.url.scheme == "https"
        or container.settings.app_public_url.startswith("https")
    )


@router.get("/_sara/login", include_in_schema=False)
async def login_page(
    request: Request,
    next_path: str = Query(default="/console", alias="next"),
    container: AppContainer = Depends(get_container),
) -> Response:
    """Show the sign-in form, or continue straight to the console when already signed in."""
    target = _safe_next(next_path)
    if console_user(request, container) is not None:
        return RedirectResponse(target, status_code=303)
    return FileResponse(
        STATIC_DIR / "login.html", media_type="text/html", headers=NO_STORE
    )


@router.post("/_sara/login", include_in_schema=False)
async def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next_path: str = Form(default="/console", alias="next"),
    container: AppContainer = Depends(get_container),
) -> Response:
    """Verify console credentials and set a signed session cookie."""
    target = _safe_next(next_path)
    client_ip = request.client.host if request.client else "unknown"
    # Peek first and consume only on a failed attempt, so the owner's own successful
    # sign-ins never spend the login budget.
    limit_key = f"login:{client_ip}"
    remaining, retry_after = container.login_limiter.peek(
        limit_key, LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW_SECONDS
    )
    if remaining <= 0:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "login_rate_limited",
                "message": "Too many sign-in attempts. Try again later.",
            },
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )
    # Compare both fields so a wrong username costs the same as a wrong password.
    user_ok = constant_time_equals(username, container.console_username)
    password_ok = constant_time_equals(password, container.console_password)
    if not (user_ok and password_ok):
        container.login_limiter.check_and_consume(
            limit_key, LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW_SECONDS
        )
        return RedirectResponse(
            f"/_sara/login?error=1&next={quote(target, safe='')}", status_code=303
        )
    token = sign_console_session(
        container.console_username,
        container.console_session_secret,
        container.settings.console_session_ttl_seconds,
    )
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=container.settings.console_session_ttl_seconds,
        httponly=True,
        path="/",
        samesite="lax",
        secure=_cookie_secure(request, container),
    )
    return response


@router.post("/_sara/logout", include_in_schema=False)
async def logout(
    request: Request, container: AppContainer = Depends(get_container)
) -> Response:
    """Clear the console session cookie and return to the sign-in page."""
    response = RedirectResponse("/_sara/login", status_code=303)
    response.delete_cookie(
        COOKIE_NAME, path="/", secure=_cookie_secure(request, container)
    )
    return response


@session_router.get("/_sara/session", summary="Whether the console has a signed-in session")
async def auth_session(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Report console authentication state; safe to call whether auth is on or off."""
    username = console_user(request, container)
    return {
        "authenticated": username is not None,
        "username": username,
        # The console uses this to send an expired session back to the sign-in page
        # instead of showing a permanent "no access" banner.
        "enabled": console_auth_enabled(container.settings),
    }
