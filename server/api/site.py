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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from server.api.dashboard import STATIC_DIR
from server.api.gateway import _raw_request_path, handle_agent_request
from server.dependencies import AppContainer, get_container
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


async def _proxy_to_origin(request: Request, path: str, container: AppContainer) -> Response:
    if container.origin_proxy is None:
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
        origin = await container.origin_proxy.forward(
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
    if container.settings.sara_mode == "demo":
        return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")
    if is_agent_request(request, container):
        return await handle_agent_request(
            request, "", container, raw_subpath=_raw_request_path(request)
        )
    return await _proxy_to_origin(request, "", container)


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
    if is_agent_request(request, container):
        return await handle_agent_request(
            request, path, container, raw_subpath=_raw_request_path(request)
        )
    if is_reserved_path(path):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"No route for /{path}."},
        )
    return await _proxy_to_origin(request, path, container)
