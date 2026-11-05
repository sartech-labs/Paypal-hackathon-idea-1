"""Live demo page and the endpoints it uses to run crawlers from the browser."""

from __future__ import annotations

import ipaddress
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from server.api.dashboard import STATIC_DIR
from server.dependencies import AppContainer, demo_access, get_container, is_valid_admin_token
from server.schemas import DemoConfigResponse, DemoRunRequest, DemoRunResponse
from server.services.demo_runner import DemoRunError

router = APIRouter(tags=["Live demo"])


def _view_access(container: AppContainer, x_admin_token: Optional[str]) -> str:
    """Who may watch runs: admins, or anyone on a public demo or public dashboard."""
    access = demo_access(container, x_admin_token)
    if access == "none" and x_admin_token is None and container.settings.dashboard_public:
        return "public"
    return access


def _require_view(container: AppContainer, x_admin_token: Optional[str]) -> str:
    access = _view_access(container, x_admin_token)
    if access == "none":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "demo_authorization_required",
                "message": "Provide a valid X-Admin-Token, or enable DEMO_PUBLIC=true for a public demo.",
            },
        )
    return access


def _internal_base_url(request: Request) -> str:
    """Loopback URL of this server, so demo crawlers do not leave the machine."""
    server = request.scope.get("server") or ("127.0.0.1", 8000)
    host, port = server[0], server[1]
    try:
        address = ipaddress.ip_address(host)
        if address.is_unspecified:
            host = "127.0.0.1" if address.version == 4 else "::1"
        if address.version == 6:
            host = f"[{host}]"
    except ValueError:
        pass  # a host name (for example the test client's "testserver")
    return f"http://{host}:{port}"


@router.get("/", include_in_schema=False)
async def demo_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")


@router.get("/demo", include_in_schema=False)
async def demo_alias() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")


@router.get("/demo/config", response_model=DemoConfigResponse, summary="Live demo capabilities and recent runs")
async def demo_config(
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoConfigResponse:
    if x_admin_token is not None and not is_valid_admin_token(container, x_admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_admin_token", "message": "The admin token was rejected."},
        )
    access = _view_access(container, x_admin_token)
    can_start = demo_access(container, x_admin_token) != "none"
    runner = container.demo_runner
    site = container.site_config
    return DemoConfigResponse(
        site_name=site.site_name,
        policy_version=site.policy_version,
        payment_mode=container.payment_provider.mode,
        paypal_autopay=container.payment_provider.autopay,
        settlement_mode=container.deposit_manager.settlement_mode,
        deposit_amount=str(site.deposit_amount),
        currency=site.currency,
        ai_available=container.ai_generator.is_available(),
        ai_provider=container.ai_generator.provider,
        ai_model=container.ai_generator.model,
        paypal_mcp_configured=container.paypal_mcp.is_configured(),
        paypal_mcp_transport=container.paypal_mcp.transport,  # type: ignore[arg-type]
        demo_public=container.settings.demo_public,
        access=access,  # type: ignore[arg-type]
        can_start_runs=can_start,
        runs_remaining_this_hour=runner.runs_remaining(),
        active_run_id=runner.active_run_id() if access != "none" else None,
        recent_runs=runner.recent() if access != "none" else [],
    )


@router.post(
    "/demo/runs",
    response_model=DemoRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a crawler run (AI agent or scripted) against this gateway",
)
async def start_run(
    payload: DemoRunRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoRunResponse:
    if demo_access(container, x_admin_token) == "none":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "demo_authorization_required",
                "message": "Starting runs needs the admin token, or DEMO_PUBLIC=true on the gateway.",
            },
        )
    generator = container.ai_generator
    try:
        run = container.demo_runner.start(
            driver=payload.driver,
            persona=payload.persona,
            base_url=_internal_base_url(request),
            ai_provider=generator.provider,
            ai_model=generator.model,
            ai_effort=container.settings.ai_effort,
            ai_available=generator.is_available(),
        )
    except DemoRunError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    return run.snapshot()


def _get_run(container: AppContainer, run_id: str):  # type: ignore[no-untyped-def]
    run = container.demo_runner.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "run_not_found", "message": f"Unknown run_id: {run_id}"},
        )
    return run


@router.get("/demo/runs/{run_id}", response_model=DemoRunResponse, summary="Run status and events after a sequence number")
async def get_run(
    run_id: str,
    after: int = Query(default=0, ge=0),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoRunResponse:
    _require_view(container, x_admin_token)
    return _get_run(container, run_id).snapshot(after=after)


@router.post("/demo/runs/{run_id}/approval", response_model=DemoRunResponse, summary="Tell a waiting run that the PayPal checkout was approved")
async def confirm_approval(
    run_id: str,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoRunResponse:
    if demo_access(container, x_admin_token) == "none":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "demo_authorization_required", "message": "Not allowed."})
    run = _get_run(container, run_id)
    container.demo_runner.confirm_approval(run_id)
    return run.snapshot(after=len(run.events))


@router.post("/demo/runs/{run_id}/cancel", response_model=DemoRunResponse, summary="Stop a running demo crawler")
async def cancel_run(
    run_id: str,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoRunResponse:
    if demo_access(container, x_admin_token) == "none":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "demo_authorization_required", "message": "Not allowed."})
    run = _get_run(container, run_id)
    container.demo_runner.cancel(run_id)
    return run.snapshot(after=len(run.events))
