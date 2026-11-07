"""PayPal webhooks: verified events keep orders, subscriptions, and deposits in sync.

Every event is checked with PayPal's verify-webhook-signature API (against the app's
webhook ID, PAYPAL_WEBHOOK_ID) before anything happens, and each event ID is processed
once. Handlers never trust the event body for money facts: a capture event makes the
server read the order back from PayPal before marking it paid, and a subscription event
makes it read the subscription's status from PayPal.

Subscribe the PayPal app's webhook (Developer Dashboard > Apps > Webhooks) to:
PAYMENT.CAPTURE.COMPLETED, PAYMENT.CAPTURE.REFUNDED, PAYMENT.CAPTURE.REVERSED,
BILLING.SUBSCRIPTION.ACTIVATED / CANCELLED / SUSPENDED / EXPIRED, PAYMENT.SALE.COMPLETED,
CUSTOMER.DISPUTE.CREATED / UPDATED / RESOLVED.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from server.dependencies import AppContainer, get_container
from server.services.payment_service import PaymentProviderError

router = APIRouter(tags=["PayPal webhooks"])
logger = logging.getLogger("agenttrust.webhooks")

MAX_BODY_BYTES = 256_000
_CAPTURE_IN_LINK = re.compile(r"/captures/([A-Za-z0-9_-]+)")


def _capture_id_from_links(resource: dict[str, Any]) -> Optional[str]:
    for link in resource.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "up":
            match = _CAPTURE_IN_LINK.search(str(link.get("href", "")))
            if match:
                return match.group(1)
    return None


async def _order_for_capture(container: AppContainer, capture_id: Optional[str]) -> Optional[str]:
    if not capture_id:
        return None
    for order in await container.commerce.list_orders():
        if order.provider_capture_id == capture_id:
            return order.order_id
    return None


async def _deposit_for_capture(container: AppContainer, capture_id: Optional[str]) -> Optional[str]:
    if not capture_id:
        return None
    for deposit in await container.deposit_manager.list_deposits():
        if deposit.provider_capture_id == capture_id:
            return deposit.deposit_id
    return None


async def _handle(container: AppContainer, event_type: str, resource: dict[str, Any]) -> str:
    commerce = container.commerce
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        custom_id = str(resource.get("custom_id") or "")
        if commerce.has_order(custom_id):
            return await commerce.confirm_order_from_webhook(custom_id)
        return "deposit_capture_noted" if custom_id.startswith("dep_") else "unknown_reference"

    if event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
        capture_id = _capture_id_from_links(resource) or (resource.get("id") if event_type.endswith("REVERSED") else None)
        order_id = await _order_for_capture(container, capture_id)
        if order_id:
            return await commerce.refund_from_webhook(order_id, resource.get("id"))
        deposit_id = await _deposit_for_capture(container, capture_id)
        if deposit_id:
            try:
                await container.deposit_manager.reconcile_refund(deposit_id)
                return "deposit_refund_reconciled"
            except Exception as exc:  # already settled, or not a pending refund
                return f"deposit_refund_noted ({type(exc).__name__})"
        return "unknown_reference"

    if event_type.startswith("BILLING.SUBSCRIPTION."):
        ref = commerce.subscription_ref_for(resource.get("custom_id"), resource.get("id"))
        return await commerce.sync_subscription_from_webhook(ref) if ref else "unknown_subscription"

    if event_type == "PAYMENT.SALE.COMPLETED":
        ref = commerce.subscription_ref_for(None, resource.get("billing_agreement_id"))
        return await commerce.subscription_payment_from_webhook(ref) if ref else "unknown_subscription"

    if event_type.startswith("CUSTOMER.DISPUTE."):
        dispute_status = str(resource.get("status") or event_type.rsplit(".", 1)[-1])
        for transaction in resource.get("disputed_transactions") or []:
            if not isinstance(transaction, dict):
                continue
            order_id = await _order_for_capture(container, transaction.get("seller_transaction_id"))
            if order_id:
                return await commerce.dispute_from_webhook(order_id, resource.get("dispute_id"), dispute_status)
        return "unknown_reference"

    return "ignored_event_type"


@router.post("/webhooks/paypal", summary="PayPal webhook receiver (signature-verified)")
async def paypal_webhook(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail={"error": "payload_too_large", "message": "Webhook payload is too large."})
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_json", "message": "Webhook body is not JSON."}) from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("event_type"):
        raise HTTPException(status_code=400, detail={"error": "invalid_event", "message": "Not a PayPal webhook event."})

    event_id = str(event["id"])[:128]
    event_type = str(event["event_type"])[:96]
    headers = {k: v for k, v in request.headers.items() if k.lower().startswith("paypal-")}
    try:
        verified = await container.payment_provider.verify_webhook_signature(
            headers=headers, event=event, webhook_id=container.settings.paypal_webhook_id
        )
    except PaymentProviderError:
        verified = False
    if not verified:
        await container.audit_logger.log_event(
            event_type="PAYPAL_WEBHOOK_REJECTED", allowed=False, reason_code="SIGNATURE_NOT_VERIFIED",
            details={"event_id": event_id, "webhook_event_type": event_type},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "invalid_signature", "message": "PayPal did not verify this webhook."})

    if await container.commerce.webhook_seen(event_id):
        return {"received": True, "event_id": event_id, "outcome": "duplicate"}

    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    try:
        outcome = await _handle(container, event_type, resource)
    except PaymentProviderError as exc:
        # Not remembered: PayPal retries the delivery and we try again.
        logger.warning("Webhook %s (%s) could not be reconciled: %s", event_id, event_type, exc)
        raise HTTPException(status_code=503, detail={"error": "reconcile_failed", "message": "Could not reach PayPal; retry later."}) from exc
    await container.commerce.remember_webhook(event_id, event_type, outcome)
    await container.audit_logger.log_event(
        event_type="PAYPAL_WEBHOOK_PROCESSED", allowed=True, reason_code=outcome[:64],
        payment_mode=container.payment_provider.mode.value,
        details={"event_id": event_id, "webhook_event_type": event_type},
    )
    return {"received": True, "event_id": event_id, "outcome": outcome}
