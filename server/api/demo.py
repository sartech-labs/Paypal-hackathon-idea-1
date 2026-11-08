"""Live demo page and the endpoints it uses to run crawlers from the browser."""

from __future__ import annotations

import html
import ipaddress
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from server.api.dashboard import STATIC_DIR
from server.core.security import utc_now_iso, validate_crawler_id
from server.dependencies import AppContainer, demo_access, get_container, is_valid_admin_token
from server.schemas import (
    DemoConfigResponse,
    DemoDecisionRequest,
    DemoRequestHistoryResponse,
    DemoRequestRow,
    DemoRunRequest,
    DemoRunResponse,
)
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
async def agent_studio_page() -> FileResponse:
    """The landing page: the personal agent paying a website four ways through PayPal."""
    return FileResponse(STATIC_DIR / "agent.html", media_type="text/html")


@router.get("/agent", include_in_schema=False)
async def agent_studio_alias() -> FileResponse:
    return FileResponse(STATIC_DIR / "agent.html", media_type="text/html")


@router.get("/website", include_in_schema=False)
async def website_page() -> FileResponse:
    """The protected website itself (Sentinel News), as a person sees it in a browser."""
    return FileResponse(STATIC_DIR / "site.html", media_type="text/html")


@router.get("/about", include_in_schema=False)
async def about_page() -> FileResponse:
    """What the demo is: the people involved, the tabs, and what is real."""
    return FileResponse(STATIC_DIR / "about.html", media_type="text/html")


_ARTICLE = re.compile(r"<article>\s*<h2>(.*?)</h2>\s*<p>(.*?)</p>\s*</article>", re.S)
_HEADING = re.compile(r"<h1>(.*?)</h1>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment)).strip()


@router.get("/website/content", summary="The website's public pages as articles, for the human-facing site")
async def website_content(container: AppContainer = Depends(get_container)) -> dict:
    """Headlines and article text from the site's allowed pages, for people reading in a browser.

    Only visible article text is returned: hidden notes in the pages (the prompt-injection traps
    that test agents) are not articles, so they never reach the human page. Disallowed pages are
    never part of the allowed set.
    """
    pages = []
    for page in container.content_service.allowed_pages():
        articles = [{"headline": _text(h), "text": _text(p)} for h, p in _ARTICLE.findall(page.body)]
        if not articles:
            continue
        heading = _HEADING.search(page.body)
        pages.append({
            "path": page.path,
            "category": page.category,
            "heading": _text(heading.group(1)) if heading else page.title,
            "articles": articles,
        })
    site = container.site_config
    return {
        "site_name": site.site_name,
        "currency": site.currency,
        "pages": pages,
        "pricing_tiers": [{"name": t.name, "price": str(t.price)} for t in container.pricing.tiers],
    }


@router.get("/crawler", include_in_schema=False)
async def demo_page() -> FileResponse:
    """The crawler deposit demo: compliant and rogue crawlers against the 402 gateway."""
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
        paypal_js_client_id=(
            container.settings.paypal_client_id or None
            if container.payment_provider.mode.value == "paypal_sandbox"
            else None
        ),
        access=access,  # type: ignore[arg-type]
        can_start_runs=can_start,
        runs_remaining_this_hour=runner.runs_remaining(),
        active_run_id=runner.active_run_id() if access != "none" else None,
        recent_runs=runner.recent() if access != "none" else [],
        pricing_tiers=container.pricing.tiers,
    )


GATEWAY_REQUEST_EVENTS = {
    "CHALLENGE_ISSUED",
    "GATEWAY_ACCESS_ALLOWED",
    "GATEWAY_ACCESS_REJECTED",
    "POLICY_VIOLATION",
}
AUDIT_SCAN_LIMIT = 5000
MAX_HISTORY_ROWS = 200


@router.get(
    "/demo/requests",
    response_model=DemoRequestHistoryResponse,
    summary="Crawler request history: every gateway request with its pricing tier, deposit, and decision",
)
async def request_history(
    crawler_id: Optional[str] = Query(default=None, max_length=128),
    limit: int = Query(default=MAX_HISTORY_ROWS, ge=1, le=MAX_HISTORY_ROWS),
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoRequestHistoryResponse:
    """Rows come from the append-only audit log joined with the stored deposit records.

    Nothing here is synthesized: a row exists only for a request the gateway answered,
    and payment fields reflect the deposit's current recorded state.
    """
    _require_view(container, x_admin_token)
    if crawler_id is not None:
        try:
            crawler_id = validate_crawler_id(crawler_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "invalid_crawler_id", "message": str(exc)},
            ) from exc
    events = await container.audit_logger.get_events(crawler_id=crawler_id, limit=AUDIT_SCAN_LIMIT)
    requests = [event for event in events if event.event_type in GATEWAY_REQUEST_EVENTS][-limit:]
    deposits = {d.deposit_id: d for d in await container.deposit_manager.list_deposits()}
    pricing = container.pricing

    rows: list[DemoRequestRow] = []
    for event in requests:
        details = event.details or {}
        deposit = deposits.get(event.deposit_id) if event.deposit_id else None
        amount = None
        if event.event_type == "CHALLENGE_ISSUED":
            http_status = 402
            decision = "payment_required"
            amount = details.get("required_amount")
            if event.challenge_id:
                challenge = await container.deposit_manager.get_challenge(event.challenge_id)
                if challenge is not None and challenge.deposit_id:
                    deposit = deposits.get(challenge.deposit_id)
        else:
            raw_status = details.get("http_status")
            http_status = int(raw_status) if isinstance(raw_status, int) else (200 if event.allowed else None)
            decision = "granted" if event.allowed else "denied"
        # Events written since tiers exist record the tier; older ones are resolved by path.
        if "pricing_tier" in details:
            tier = pricing.get(details.get("pricing_tier"))
        else:
            tier = pricing.tier_for_request_path(event.path) if event.path else None
        if deposit is not None and amount is None:
            amount = deposit.required_amount
        rows.append(
            DemoRequestRow(
                request_id=event.event_id,
                timestamp=event.timestamp,
                crawler_id=event.crawler_id,
                resource=event.path,
                pricing_tier=tier.tier_id if tier else None,
                pricing_tier_name=tier.name if tier else None,
                deposit_id=deposit.deposit_id if deposit else None,
                deposit_amount=amount,
                currency=(deposit.currency if deposit else details.get("currency")) or container.site_config.currency,
                http_status=http_status,
                payment_status=deposit.payment_status.value if deposit else None,
                deposit_state=deposit.deposit_state.value if deposit else None,
                access_decision=decision,  # type: ignore[arg-type]
                reason_code=event.reason_code,
                simulated_payment=(deposit.payment_mode.value == "mock") if deposit else None,
                provider_order_id=deposit.provider_order_id if deposit else None,
            )
        )
    rows.reverse()  # newest first
    return DemoRequestHistoryResponse(rows=rows, generated_at=utc_now_iso())


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
    tier = None
    if payload.tier is not None:
        tier = container.pricing.get(payload.tier)
        if tier is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "unknown_pricing_tier", "message": f"Unknown pricing tier: {payload.tier}"},
            )
    try:
        run = container.demo_runner.start(
            driver=payload.driver,
            persona=payload.persona,
            base_url=_internal_base_url(request),
            ai_provider=generator.provider,
            ai_model=generator.model,
            ai_effort=container.settings.ai_effort,
            ai_available=generator.is_available(),
            tier=tier,
            task=payload.task,
            history=[(turn.role, turn.content) for turn in payload.history],
            budget=payload.budget,
            approval_threshold=payload.approval_threshold,
            allowed_types=list(payload.allowed_types) if payload.allowed_types else None,
            max_per_purchase=payload.max_per_purchase,
            allowed_merchants=payload.allowed_merchants,
            purchase_review=payload.purchase_review,
            save_payment_method=payload.save_payment_method,
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


@router.post(
    "/demo/runs/{run_id}/decision",
    response_model=DemoRunResponse,
    summary="Approve or decline the purchase the personal agent is waiting on",
)
async def decide_purchase(
    run_id: str,
    payload: DemoDecisionRequest,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DemoRunResponse:
    if demo_access(container, x_admin_token) == "none":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "demo_authorization_required", "message": "Not allowed."})
    run = _get_run(container, run_id)
    if not container.demo_runner.decide(run_id, payload.approve):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_pending_decision", "message": "This run is not waiting for a decision."},
        )
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
