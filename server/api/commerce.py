"""Agent commerce API: offer discovery, checkout, subscriptions, and paid content.

One machine-readable catalog (`/.well-known/agent-commerce.json`) lists every way an AI
agent can pay this site through PayPal: refundable crawl deposits, pay-per-access items,
subscriptions, and products. Paid content lives under `/paid/...`; without a token that
covers it the site answers HTTP 402 with the offers that unlock it.
"""

from __future__ import annotations

import html
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response

from server.api.dashboard import _dashboard_access
from server.core.security import utc_now_iso, validate_crawler_id
from server.dependencies import AppContainer, extract_optional_bearer_token, get_container, is_valid_admin_token
from server.schemas import (
    AgentActionRequest,
    CheckoutRequest,
    CommerceOrderResponse,
    RefundOrderRequest,
    SubscribeRequest,
    TrackingRequest,
    SubscriptionResponse,
)
from server.services.commerce import CommerceError

router = APIRouter(tags=["Agent commerce"])


def _agent_id(raw: Optional[str]) -> str:
    try:
        return validate_crawler_id(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_agent_id", "message": str(exc)},
        ) from exc


def _raise(exc: CommerceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": str(exc)}) from exc


def build_offer_catalog(container: AppContainer) -> dict[str, Any]:
    """Everything an agent needs to discover offers and pay for them."""
    site = container.site_config
    commerce = container.commerce
    return {
        "protocol": "sara/1.0",
        "merchant": commerce.catalog.merchant_name,
        "site_id": site.site_id,
        "currency": site.currency,
        "payments": {
            "processor": "PayPal",
            "mode": container.payment_provider.mode.value,
            "simulated": container.payment_provider.mode.value == "mock",
            "apis": ["Orders v2", "Payments v2", "Subscriptions v1"],
        },
        "offers": {
            "crawl_deposits": {
                "description": (
                    "Refundable deposit to crawl protected pages under a published policy. "
                    "Returned in full when the crawler follows the rules; forfeited on violations."
                ),
                "policy_version": site.policy_version,
                "policy": "/policies",
                "entry_point": "GET /gateway/{path} (answers 402 with a challenge)",
                "tiers": [
                    {
                        "tier_id": tier.tier_id,
                        "name": tier.name,
                        "price": tier.price,
                        "currency": tier.currency,
                        "refundable": True,
                        "paths": [tier.path_prefix, *tier.additional_path_prefixes],
                    }
                    for tier in container.pricing.tiers
                ],
            },
            **commerce.catalog.public_offers(),
        },
        "endpoints": {
            "catalog": "GET /.well-known/agent-commerce.json",
            "checkout": "POST /commerce/checkout {agent_id, offer_type: access|product, item_id | items:[{sku, quantity}], purpose}",
            "capture": "POST /commerce/orders/{order_id}/capture {agent_id}",
            "cancel_order": "POST /commerce/orders/{order_id}/cancel {agent_id}",
            "subscribe": "POST /commerce/subscriptions {agent_id, plan_id, purpose}",
            "activate_subscription": "POST /commerce/subscriptions/{subscription_ref}/activate {agent_id}",
            "cancel_subscription": "POST /commerce/subscriptions/{subscription_ref}/cancel {agent_id}",
            "paid_content": "GET /paid/{path} with Authorization: Bearer <access_token>",
        },
        "rules": [
            "Prices are set by the site; checkout requests name offers by ID only.",
            "Payment is verified with PayPal before access or fulfilment.",
            "Access tokens are returned once and unlock only what was bought.",
            "Subscriptions are recurring charges and need the payer's approval at PayPal.",
        ],
        "generated_at": utc_now_iso(),
    }


@router.get("/.well-known/agent-commerce.json", summary="Machine-readable offer catalog for AI agents")
async def well_known_catalog(container: AppContainer = Depends(get_container)) -> JSONResponse:
    return JSONResponse(build_offer_catalog(container), headers={"Cache-Control": "no-store"})


@router.get("/commerce/offers", summary="Offer catalog (alias of /.well-known/agent-commerce.json)")
async def offers(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return build_offer_catalog(container)


@router.post(
    "/commerce/checkout",
    response_model=CommerceOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a PayPal order for a pay-per-access item or a product cart",
)
async def checkout(payload: CheckoutRequest, container: AppContainer = Depends(get_container)) -> CommerceOrderResponse:
    agent_id = _agent_id(payload.agent_id)
    try:
        record = await container.commerce.create_order(
            agent_id=agent_id,
            offer_type=payload.offer_type,
            item_id=payload.item_id,
            items=[(line.sku, line.quantity) for line in payload.items],
            purpose=payload.purpose,
            buyer=payload.buyer,
            save_payment_method=payload.save_payment_method,
            payment_method_id=payload.payment_method_id,
        )
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.order_response(record)


@router.post(
    "/commerce/orders/{order_id}/capture",
    response_model=CommerceOrderResponse,
    summary="Capture and verify the order with PayPal; returns the access token once",
)
async def capture(order_id: str, payload: AgentActionRequest, container: AppContainer = Depends(get_container)) -> CommerceOrderResponse:
    agent_id = _agent_id(payload.agent_id)
    try:
        record, token = await container.commerce.capture_order(order_id=order_id, agent_id=agent_id)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.order_response(record, access_token=token)


@router.post(
    "/commerce/orders/{order_id}/cancel",
    response_model=CommerceOrderResponse,
    summary="Cancel an unpaid order (no money moved)",
)
async def cancel_order(order_id: str, payload: AgentActionRequest, container: AppContainer = Depends(get_container)) -> CommerceOrderResponse:
    agent_id = _agent_id(payload.agent_id)
    try:
        record = await container.commerce.cancel_order(order_id=order_id, agent_id=agent_id)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.order_response(record)


def _require_admin(container: AppContainer, x_admin_token: Optional[str]) -> None:
    if not is_valid_admin_token(container, x_admin_token):
        raise HTTPException(status_code=401, detail={"error": "admin_required", "message": "This publisher action needs the admin token."})


@router.post("/commerce/orders/{order_id}/refund", response_model=CommerceOrderResponse, summary="Publisher: refund a paid order through PayPal")
async def refund_order(
    order_id: str,
    payload: RefundOrderRequest,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> CommerceOrderResponse:
    _require_admin(container, x_admin_token)
    try:
        record = await container.commerce.refund_order(order_id=order_id, reason=payload.reason)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.order_response(record)


@router.post("/commerce/orders/{order_id}/tracking", response_model=CommerceOrderResponse, summary="Publisher: add shipment tracking at PayPal")
async def add_tracking(
    order_id: str,
    payload: TrackingRequest,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> CommerceOrderResponse:
    _require_admin(container, x_admin_token)
    try:
        record = await container.commerce.add_tracking(order_id=order_id, tracking_number=payload.tracking_number, carrier=payload.carrier)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.order_response(record)


@router.get("/commerce/orders/{order_id}", response_model=CommerceOrderResponse, summary="Order status")
async def get_order(
    order_id: str,
    x_agent_id: Optional[str] = Header(default=None),
    container: AppContainer = Depends(get_container),
) -> CommerceOrderResponse:
    try:
        record = await container.commerce.get_order(order_id, _agent_id(x_agent_id))
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.order_response(record)


@router.post(
    "/commerce/subscriptions",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a PayPal subscription to a plan",
)
async def subscribe(payload: SubscribeRequest, container: AppContainer = Depends(get_container)) -> SubscriptionResponse:
    agent_id = _agent_id(payload.agent_id)
    try:
        record, token = await container.commerce.subscribe(agent_id=agent_id, plan_id=payload.plan_id, purpose=payload.purpose)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.subscription_response(record, access_token=token)


@router.post(
    "/commerce/subscriptions/{subscription_ref}/activate",
    response_model=SubscriptionResponse,
    summary="Confirm the subscription is ACTIVE at PayPal; returns the access token once",
)
async def activate(subscription_ref: str, payload: AgentActionRequest, container: AppContainer = Depends(get_container)) -> SubscriptionResponse:
    agent_id = _agent_id(payload.agent_id)
    try:
        record, token = await container.commerce.activate_subscription(ref=subscription_ref, agent_id=agent_id)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.subscription_response(record, access_token=token)


@router.post(
    "/commerce/subscriptions/{subscription_ref}/cancel",
    response_model=SubscriptionResponse,
    summary="Cancel the subscription at PayPal",
)
async def cancel(subscription_ref: str, payload: AgentActionRequest, container: AppContainer = Depends(get_container)) -> SubscriptionResponse:
    agent_id = _agent_id(payload.agent_id)
    try:
        record = await container.commerce.cancel_subscription(ref=subscription_ref, agent_id=agent_id)
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.subscription_response(record)


@router.get("/commerce/subscriptions/{subscription_ref}", response_model=SubscriptionResponse, summary="Subscription status")
async def get_subscription(
    subscription_ref: str,
    x_agent_id: Optional[str] = Header(default=None),
    container: AppContainer = Depends(get_container),
) -> SubscriptionResponse:
    try:
        record = await container.commerce.get_subscription(subscription_ref, _agent_id(x_agent_id))
    except CommerceError as exc:
        _raise(exc)
    return container.commerce.subscription_response(record)


@router.get("/paid/{path:path}", summary="Paid content: media, members-only pages, and downloads")
async def paid_content(
    path: str,
    container: AppContainer = Depends(get_container),
    token: Optional[str] = Depends(extract_optional_bearer_token),
) -> Response:
    normalized = "/" + "/".join(segment for segment in path.split("/") if segment)
    if ".." in path or "%" in path or len(normalized) > 200:
        raise HTTPException(status_code=400, detail={"error": "invalid_path", "message": "Invalid content path."})
    commerce = container.commerce
    page = commerce.catalog.page_for(normalized)
    if page is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"No paid content at {normalized}."})

    allowed, grant, reason = await commerce.authorize(token, normalized)
    agent_id = grant.agent_id if grant else None
    if not allowed:
        await container.audit_logger.log_event(
            event_type="PAID_CONTENT_DENIED",
            crawler_id=agent_id,
            path=normalized,
            method="GET",
            allowed=False,
            reason_code=reason,
        )
        offers = commerce.offers_for_path(normalized)
        return JSONResponse(
            status_code=402,
            content={
                "error": "payment_required",
                "reason_code": reason,
                "message": f"{page.title} is paid content. Buy one of the offers below through PayPal.",
                "resource": normalized,
                "offers": offers,
                "catalog": "/.well-known/agent-commerce.json",
            },
        )
    await container.audit_logger.log_event(
        event_type="PAID_CONTENT_SERVED",
        crawler_id=agent_id,
        path=normalized,
        method="GET",
        allowed=True,
        reason_code="ALLOWED",
        details={"grant": grant.kind if grant else None, "ref": grant.ref if grant else None},
    )
    return Response(
        content=page.body,
        media_type=page.content_type.split(";")[0],
        headers={"X-SARA-Grant": grant.kind if grant else "", "Cache-Control": "no-store"},
    )


def _callback_page(title: str, message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>body{{font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem}}</style>"
        f"</head><body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )


@router.get("/commerce/paypal/return", include_in_schema=False)
async def paypal_return(
    order_id: Optional[str] = Query(default=None, max_length=64),
    subscription_ref: Optional[str] = Query(default=None, max_length=64),
) -> HTMLResponse:
    what = "subscription" if subscription_ref else "order"
    return _callback_page("Approved at PayPal", f"Your {what} was approved. You can close this window; the agent continues on its own.")


@router.get("/commerce/paypal/cancel", include_in_schema=False)
async def paypal_cancel() -> HTMLResponse:
    return _callback_page("Checkout cancelled", "Nothing was charged. You can close this window.")


@router.get("/commerce/summary", summary="Publisher view: revenue by payment type, orders and subscriptions")
async def commerce_summary(
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _dashboard_access(container, x_admin_token, request)
    data = await container.commerce.summary()
    deposits = await container.deposit_manager.list_deposits()
    forfeited = [d for d in deposits if d.deposit_state.value == "FORFEITED"]
    returned = [d for d in deposits if d.deposit_state.value in ("REFUNDED", "RELEASED")]
    data["deposits"] = {
        "total": len(deposits),
        "returned": len(returned),
        "forfeited": len(forfeited),
        "forfeited_revenue": f"{sum(Decimal(d.captured_amount or d.required_amount) for d in forfeited):.2f}",
    }
    data["generated_at"] = utc_now_iso()
    return data
