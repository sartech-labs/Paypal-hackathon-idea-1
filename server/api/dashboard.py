"""Publisher dashboard: the web console at /console and its read-only data endpoints."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from server.api.auth import console_locked
from server.api.deposits import build_deposit_explanation
from server.core.security import utc_now_iso
from server.dependencies import AppContainer, get_container, is_valid_admin_token
from server.schemas import (
    DashboardDataResponse,
    DashboardDepositRow,
    DashboardEventRow,
    DashboardKpis,
    DashboardSessionRow,
    DepositExplanationResponse,
    DepositRecord,
    DepositState,
    PolicyDecisionStatus,
    SessionStatus,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_EVENTS = 250

router = APIRouter(tags=["Dashboard"])


def _dashboard_access(container: AppContainer, x_admin_token: Optional[str]) -> str:
    """Return 'admin' or 'public', or raise 401 when neither applies."""
    if is_valid_admin_token(container, x_admin_token):
        return "admin"
    if x_admin_token is None and (
        container.settings.dashboard_public or container.config_public
    ):
        return "public"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "dashboard_authorization_required",
            "message": (
                "Provide a valid X-Admin-Token, or enable DASHBOARD_PUBLIC=true for a "
                "read-only demo deployment."
            ),
        },
    )


def _is_held(deposit: DepositRecord) -> bool:
    """Funds are held for a live session with no decision yet."""
    return (
        deposit.deposit_state in (DepositState.CAPTURED, DepositState.AUTHORIZED)
        and deposit.policy_decision == PolicyDecisionStatus.UNDECIDED
    )


def _needs_attention(deposit: DepositRecord) -> bool:
    """A decision exists but settlement with the provider is not confirmed, or it failed."""
    if deposit.deposit_state in (DepositState.REFUND_PENDING, DepositState.FAILED):
        return True
    return (
        deposit.deposit_state in (DepositState.CAPTURED, DepositState.AUTHORIZED)
        and deposit.policy_decision != PolicyDecisionStatus.UNDECIDED
    )


def console_page(request: Request, container: AppContainer) -> Response:
    """The console for a signed-in owner; the sign-in page otherwise (when sign-in is on)."""
    if console_locked(request, container):
        return RedirectResponse("/signin", status_code=status.HTTP_303_SEE_OTHER)
    return FileResponse(
        STATIC_DIR / "dashboard.html", media_type="text/html", headers={"Cache-Control": "no-store"}
    )


@router.get("/console", include_in_schema=False)
async def dashboard_page(request: Request, container: AppContainer = Depends(get_container)) -> Response:
    return console_page(request, container)


@router.get("/dashboard", include_in_schema=False)
async def dashboard_alias(request: Request, container: AppContainer = Depends(get_container)) -> Response:
    return console_page(request, container)


@router.get(
    "/dashboard/data",
    response_model=DashboardDataResponse,
    summary="Dashboard data: KPIs, deposits, sessions, and recent audit events",
)
async def dashboard_data(
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DashboardDataResponse:
    access = _dashboard_access(container, x_admin_token)
    deposits = await container.deposit_manager.list_deposits()
    sessions = await container.session_manager.list_sessions()
    summary = await container.audit_logger.build_summary(
        deposits=deposits, sessions=sessions, scope="global"
    )
    events = await container.audit_logger.get_events(limit=MAX_EVENTS)

    returned = [d for d in deposits if d.deposit_state in (DepositState.REFUNDED, DepositState.RELEASED)]
    forfeited = [d for d in deposits if d.deposit_state == DepositState.FORFEITED]
    amount_returned = sum((Decimal(d.required_amount) for d in returned), Decimal("0"))
    amount_forfeited = sum(
        (Decimal(d.captured_amount or d.required_amount) for d in forfeited), Decimal("0")
    )

    kpis = DashboardKpis(
        sessions_total=len(sessions),
        sessions_active=sum(1 for s in sessions if s.status == SessionStatus.ACTIVE),
        deposits_total=len(deposits),
        deposits_held=sum(1 for d in deposits if _is_held(d)),
        deposits_returned=len(returned),
        deposits_forfeited=len(forfeited),
        deposits_needing_attention=sum(1 for d in deposits if _needs_attention(d)),
        amount_returned=f"{amount_returned:.2f}",
        amount_forfeited=f"{amount_forfeited:.2f}",
        currency=container.site_config.currency,
        gateway_requests_allowed=summary.allowed_gateway_requests,
        gateway_requests_rejected=summary.rejected_gateway_requests,
        policy_violations=summary.policy_violations,
        violations_by_reason=dict(
            sorted(summary.violation_counts_by_reason.items(), key=lambda item: (-item[1], item[0]))
        ),
    )

    return DashboardDataResponse(
        site_id=container.site_config.site_id,
        site_name=container.site_config.site_name,
        policy_version=container.site_config.policy_version,
        sara_mode=container.settings.sara_mode,
        origin_url=container.runtime_config_response().origin_url,
        origin_configured=container.origin_proxy is not None,
        config_public=container.config_public,
        payment_mode=container.payment_provider.mode,
        settlement_mode=container.deposit_manager.settlement_mode,
        ai_available=container.ai_generator.is_available(),
        ai_provider=container.ai_generator.provider,
        ai_model=container.ai_generator.model,
        access=access,  # type: ignore[arg-type]
        kpis=kpis,
        deposits=[
            DashboardDepositRow(
                deposit_id=d.deposit_id,
                crawler_id=d.crawler_id,
                session_id=d.session_id,
                amount=d.required_amount,
                currency=d.currency,
                payment_mode=d.payment_mode,
                settlement_mode=d.settlement_mode,
                deposit_state=d.deposit_state,
                payment_status=d.payment_status,
                policy_decision=d.policy_decision,
                refund_status=d.refund_status,
                created_at=d.created_at,
                updated_at=d.updated_at,
                provider_order_id=d.provider_order_id,
                provider_capture_id=d.provider_capture_id,
                provider_authorization_id=d.provider_authorization_id,
                provider_refund_id=d.provider_refund_id,
                pricing_tier=d.pricing_tier,
                pricing_tier_name=d.pricing_tier_name,
                requested_resource=d.requested_resource,
            )
            for d in sorted(deposits, key=lambda record: record.created_at, reverse=True)
        ],
        sessions=[
            DashboardSessionRow(
                session_id=s.session_id,
                crawler_id=s.crawler_id,
                deposit_id=s.deposit_id,
                status=s.status,
                total_requests=s.total_requests,
                allowed_requests=s.allowed_requests,
                rejected_requests=s.rejected_requests,
                total_violations=s.total_violations,
                disqualified=s.disqualified,
                created_at=s.created_at,
                completed_at=s.completed_at,
            )
            for s in sorted(sessions, key=lambda record: record.created_at, reverse=True)
        ],
        events=[
            DashboardEventRow(
                event_id=e.event_id,
                timestamp=e.timestamp,
                event_type=e.event_type,
                crawler_id=e.crawler_id,
                session_id=e.session_id,
                deposit_id=e.deposit_id,
                path=e.path,
                reason_code=e.reason_code,
                allowed=e.allowed,
            )
            for e in reversed(events)
        ],
        generated_at=utc_now_iso(),
    )


@router.get(
    "/dashboard/deposits/{deposit_id}/explanation",
    response_model=DepositExplanationResponse,
    summary="Dashboard view of a deposit explanation (AI for admins, or once the outcome is final)",
)
async def dashboard_explanation(
    deposit_id: str,
    use_ai: bool = Query(default=True),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DepositExplanationResponse:
    access = _dashboard_access(container, x_admin_token)
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )
    return await build_deposit_explanation(
        container, deposit, use_ai=use_ai, privileged=access == "admin"
    )
