"""Transparent site edge: gate automated clients, proxy real browsers to the origin.

In `SARA_MODE=gateway` the gateway sits at the site root. Human traffic is forwarded to
the owner's website unchanged; requests from automated clients go through the same
402 / deposit / policy pipeline as `/gateway/*`.

Classification fails closed and reads one header: the User-Agent. A request counts as human
only when that header carries a token real desktop and mobile browsers send. Anything else —
a crawler, a bot, `curl`, `python-httpx`, an empty User-Agent, an unknown client — is treated
as an agent and must pay the deposit. A Web Bot Auth signature always counts as an agent.

In `SARA_MODE=demo` the site root is the owner console, and only agent requests (and
other non-root paths) are forwarded.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from server.api.dashboard import NO_STORE, STATIC_DIR
from server.api.gateway import _raw_request_path, handle_agent_request
from server.dependencies import AppContainer, console_gate_redirect, get_container
from server.services.origin_proxy import OriginError

router = APIRouter(tags=["Site edge"])

# User-Agent tokens that real desktop and mobile browsers send. This is an allow-list of
# browsers, not a deny-list of bots: the edge fails closed, so a client that cannot show
# one of these tokens is treated as an automated agent.
BROWSER_USER_AGENT_TOKENS = (
    "chrome/",
    "chromium/",
    "crios/",
    "firefox/",
    "fxios/",
    "edg/",
    "edga/",
    "edgios/",
    "opr/",
    "opera",
    "samsungbrowser/",
    "ucbrowser/",
    "vivaldi/",
    "yabrowser/",
    "safari/",
    "gecko/",
    "trident/",
    "msie ",
)

# Path prefixes owned by SARA's own API and console. In gateway mode these must never fall
# through to the origin website: an unimplemented path under them is a SARA 404, not the
# owner's page (otherwise /admin, /sessions, and friends would leak to the origin).
RESERVED_PREFIXES = (
    "_site",
    "admin",
    "agent",
    "audit",
    "commerce",
    "console",
    "dashboard",
    "demo",
    "deposits",
    "health",
    "mcp",
    "playground",
    "policies",
    "paid",
    "paypal",
    "robots.txt",
    "redoc",
    "sessions",
    "webhooks",
    "docs",
    ".well-known",
    "openapi.json",
)


def is_agent_request(request: Request, container: AppContainer) -> bool:
    """Return True unless the request presents a recognised browser User-Agent.

    Fails closed: anything unrecognised is an agent.
    """
    headers = request.headers
    if headers.get("signature-input") or headers.get("signature"):
        return True
    if not container.settings.detect_agent_user_agents:
        return False
    # One rule: a browser fingerprint passes as human, everything else (including an empty
    # User-Agent) is an agent. No deny-list to keep in sync.
    ua = (headers.get("user-agent") or "").strip().lower()
    return not any(token in ua for token in BROWSER_USER_AGENT_TOKENS)


def is_reserved_path(path: str) -> bool:
    """True when the path belongs to SARA's own API/console surface."""
    head = path.split("/", 1)[0]
    return head in RESERVED_PREFIXES


def _runtime_for_request(
    request: Request, container: AppContainer, site_id: Optional[str] = None
):  # type: ignore[no-untyped-def]
    # Gateway mode routes by Host; demo mode always uses the console's active site.
    host = (
        request.headers.get("host")
        if container.settings.sara_mode != "demo"
        else None
    )
    return container.resolve_site(
        site_id=site_id or request.headers.get("x-sara-site"),
        host=host,
    )


def _site_raw_subpath(request: Request, path: str) -> str:
    """The un-normalized path after /_site/{site_id}, for traversal checks."""
    raw = _raw_request_path(request)
    parts = raw.split("/", 3)
    if len(parts) >= 3 and parts[1] == "_site":
        rest = parts[3] if len(parts) > 3 else ""
        return "/" + rest if rest else "/"
    return "/" + path.lstrip("/") if path else "/"


async def _proxy_to_origin(
    request: Request, path: str, container: AppContainer, runtime
) -> Response:  # type: ignore[no-untyped-def]
    if runtime.origin_proxy is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "origin_not_configured",
                "message": (
                    "Set the target website URL in the owner console, or run with "
                    "SARA_MODE=demo to use the console as the landing page."
                ),
            },
        )
    try:
        body = await request.body() if request.method not in ("GET", "HEAD") else b""
        origin = await runtime.origin_proxy.forward(
            request.method,
            "/" + path.lstrip("/"),
            request.url.query,
            headers=dict(request.headers),
            body=body,
        )
    except OriginError as exc:
        return JSONResponse(status_code=502, content={"error": "origin_unavailable", "message": str(exc)})
    return Response(
        content=origin.body if request.method != "HEAD" else b"",
        status_code=origin.status_code,
        media_type=origin.content_type,
        headers=origin.headers,
    )


@router.get("/", include_in_schema=False)
async def site_root(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Response:
    agent = is_agent_request(request, container)
    explicit_site = request.headers.get("x-sara-site")
    # The demo landing page is the console for people and for tools that do not name a
    # website. A request that explicitly names a site (the playground always does) still
    # goes through the normal gate/proxy path, so "/" works for every configured site.
    if container.settings.sara_mode == "demo" and not explicit_site:
        redirect = console_gate_redirect(request, container, request.url.path)
        if redirect is not None:
            return redirect
        return FileResponse(
            STATIC_DIR / "dashboard.html", media_type="text/html", headers=NO_STORE
        )
    try:
        runtime = _runtime_for_request(request, container)
    except KeyError as exc:
        return JSONResponse(status_code=404, content={"error": "site_not_found", "message": str(exc)})
    if runtime.site.status == "paused":
        return JSONResponse(status_code=503, content={"error": "site_paused"})
    if agent:
        return await handle_agent_request(
            request,
            "",
            container,
            raw_subpath=_raw_request_path(request),
            site_id=runtime.site.site_id,
        )
    return await _proxy_to_origin(request, "", container, runtime)


async def _handle_site_scoped(
    request: Request,
    site_id: str,
    path: str,
    container: AppContainer,
) -> Response:
    try:
        runtime = container.resolve_site(site_id=site_id)
    except KeyError as exc:
        return JSONResponse(status_code=404, content={"error": "site_not_found", "message": str(exc)})
    if runtime.site.status == "paused":
        return JSONResponse(status_code=503, content={"error": "site_paused"})
    if is_agent_request(request, container):
        return await handle_agent_request(
            request,
            path,
            container,
            raw_subpath=_site_raw_subpath(request, path),
            site_id=runtime.site.site_id,
        )
    return await _proxy_to_origin(request, path, container, runtime)


@router.api_route(
    "/_site/{site_id}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def site_scoped_root(
    site_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Response:
    return await _handle_site_scoped(request, site_id, "", container)


@router.api_route(
    "/_site/{site_id}/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def site_scoped_edge(
    site_id: str,
    path: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Response:
    return await _handle_site_scoped(request, site_id, path, container)

@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def site_edge(
    path: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Response:
    agent = is_agent_request(request, container)
    # SARA's own reserved surface is a hard 404, even when the active site is paused.
    if not agent and is_reserved_path(path):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"No route for /{path}."},
        )
    try:
        runtime = _runtime_for_request(request, container)
    except KeyError as exc:
        return JSONResponse(status_code=404, content={"error": "site_not_found", "message": str(exc)})
    if runtime.site.status == "paused":
        return JSONResponse(status_code=503, content={"error": "site_paused"})
    if agent:
        return await handle_agent_request(
            request,
            path,
            container,
            raw_subpath=_raw_request_path(request),
            site_id=runtime.site.site_id,
        )
    return await _proxy_to_origin(request, path, container, runtime)
