"""Curated crawler playground API."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from server.api.demo import _internal_base_url, _require_view
from server.dependencies import AppContainer, demo_access, get_container
from server.schemas import (
    PlaygroundConfigResponse,
    PlaygroundRecentRun,
    PlaygroundRunRequest,
    PlaygroundRunResponse,
)
from server.services.playground import (
    PlaygroundActionError,
    PlaygroundBusyError,
    PlaygroundQuotaError,
    PlaygroundUnavailableError,
    action_infos,
    default_action_ids,
)

router = APIRouter(prefix="/playground", tags=["Crawler playground"])


@router.get("/actions", response_model=PlaygroundConfigResponse, summary="List playground actions and agent availability")
async def list_actions(
    request: Request,
    site_id: Optional[str] = Query(default=None),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PlaygroundConfigResponse:
    access = _require_view(container, x_admin_token, request)
    try:
        runtime = container.resolve_site(site_id=site_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
    site = runtime.site_config
    runner = container.playground_runner
    return PlaygroundConfigResponse(
        actions=action_infos(),
        can_start=access != "none",
        runs_remaining_this_hour=runner.runs_remaining(),
        recent_runs=runner.recent() if access != "none" else [],
        ai=runner.ai_info(),
        max_actions=runner.max_actions,
        default_action_ids=default_action_ids(),
        limits={
            "max_requests_per_session": site.max_requests_per_session,
            "rate_limit_requests": site.rate_limit_requests,
            "rate_limit_window_seconds": site.rate_limit_window_seconds,
            "deposit_amount": site.deposit_amount,
            "currency": site.currency,
            "allowed_paths": list(site.allowed_paths),
            "disallowed_paths": list(site.disallowed_paths),
            "pricing_tiers": [tier.model_dump(mode="json") for tier in site.pricing_tiers],
            "policy_version": site.policy_version,
        },
    )


@router.post(
    "/runs",
    response_model=PlaygroundRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the selected good/bad playground actions with the agent",
)
async def start_run(
    payload: PlaygroundRunRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PlaygroundRunResponse:
    if demo_access(container, x_admin_token, request) == "none":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "playground_authorization_required",
                "message": "Starting playground runs needs the admin token, or DEMO_PUBLIC=true.",
            },
        )
    try:
        runtime = container.resolve_site(site_id=payload.site_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "site_not_found", "message": str(exc)},
        ) from exc
    site = runtime.site_config
    loop = asyncio.get_running_loop()

    def audit(event_type: str, reason_code: str, details: dict) -> Optional[str]:
        # A run's audit event must never claim a refused run was allowed: derive `allowed`
        # from the run outcome (unknown only while the run is still starting).
        outcome = details.get("outcome")
        allowed = True if outcome is None else outcome == "allowed"
        future = asyncio.run_coroutine_threadsafe(
            container.audit_logger.log_event(
                event_type=event_type,
                allowed=allowed,
                reason_code=reason_code,
                policy_version=site.policy_version,
                payment_mode=container.payment_provider.mode.value,
                details={**details, "playground_run_id": details.get("run_id"), "site_id": runtime.site.site_id},
            ),
            loop,
        )
        return future.result(timeout=10).event_id

    try:
        run = container.playground_runner.start(
            action_id=payload.action_id.strip() if payload.action_id else None,
            action_ids=(payload.action_ids or None),
            driver=payload.driver,
            base_url=_internal_base_url(request),
            policy=site.model_dump(mode="json"),
            audit=audit,
            policy_version=site.policy_version,
            payment_mode=container.payment_provider.mode.value,
            site_id=runtime.site.site_id,
        )
    except (
        PlaygroundActionError,
        PlaygroundBusyError,
        PlaygroundQuotaError,
        PlaygroundUnavailableError,
    ) as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": str(exc)}) from exc
    return run.snapshot()


def _get_run(container: AppContainer, run_id: str):  # type: ignore[no-untyped-def]
    run = container.playground_runner.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "playground_run_not_found", "message": f"Unknown run_id: {run_id}"},
        )
    return run


@router.get("/runs/{run_id}", response_model=PlaygroundRunResponse, summary="Poll one playground run")
async def get_run(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PlaygroundRunResponse:
    _require_view(container, x_admin_token, request)
    return _get_run(container, run_id).snapshot(after=after)


@router.get("/runs", response_model=list[PlaygroundRecentRun], summary="Recent playground runs")
async def recent_runs(
    request: Request,
    limit: int = Query(default=8, ge=1, le=20),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> list[PlaygroundRecentRun]:
    _require_view(container, x_admin_token, request)
    return container.playground_runner.recent(limit=limit)


@router.post("/runs/{run_id}/cancel", response_model=PlaygroundRunResponse, summary="Stop a playground run")
async def cancel_run(
    run_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PlaygroundRunResponse:
    if demo_access(container, x_admin_token, request) == "none":
        raise HTTPException(status_code=401, detail={"error": "playground_authorization_required"})
    run = _get_run(container, run_id)
    container.playground_runner.cancel(run_id)
    return run.snapshot(after=len(run.steps))
