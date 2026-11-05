"""Deposit creation, payment capture, verification, and refund endpoints."""

from __future__ import annotations

import html
from typing import Literal, Optional, Union

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from server.core.security import StateTransitionError
from server.dependencies import (
    AppContainer,
    extract_optional_bearer_token,
    get_container,
    is_valid_admin_token,
)
from server.schemas import (
    DepositActionRequest,
    DepositCreateRequest,
    DepositExplanationResponse,
    DepositRecord,
    DepositResponse,
    PaymentMode,
    PaymentStatus,
    PayPalApprovalCallbackResponse,
)
from server.services.explainer import FINAL_DEPOSIT_STATES
from server.services.payment_service import (
    PaymentApprovalRequiredError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)

router = APIRouter(prefix="/deposits", tags=["Deposits & Payments"])


async def _get_paypal_callback_deposit(
    *,
    deposit_id: str,
    order_id: str,
    container: AppContainer,
) -> DepositRecord:
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": "Unknown deposit_id."},
        )
    if deposit.payment_mode != PaymentMode.PAYPAL_SANDBOX:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "unsupported_payment_mode", "message": "This is not a PayPal deposit."},
        )
    if not deposit.provider_order_id or order_id != deposit.provider_order_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "paypal_order_mismatch",
                "message": "The returned PayPal order does not match this deposit.",
            },
        )
    return deposit


def _paypal_callback_response(
    deposit: DepositRecord,
    outcome: Literal["CAPTURED", "AUTHORIZED", "CANCELLED", "ALREADY_CAPTURED", "ALREADY_AUTHORIZED"],
) -> PayPalApprovalCallbackResponse:
    return PayPalApprovalCallbackResponse(
        outcome=outcome,
        deposit_id=deposit.deposit_id,
        payment_status=deposit.payment_status,
        deposit_state=deposit.deposit_state,
        provider_order_id=deposit.provider_order_id or "",
        provider_capture_id=deposit.provider_capture_id,
        provider_authorization_id=deposit.provider_authorization_id,
        captured_amount=deposit.captured_amount,
        currency=deposit.currency,
    )


_CALLBACK_MESSAGES = {
    "CAPTURED": ("Deposit paid", "PayPal captured the refundable deposit. The crawler can continue."),
    "AUTHORIZED": ("Deposit hold placed", "PayPal authorized the refundable deposit hold. The crawler can continue."),
    "ALREADY_CAPTURED": ("Deposit already paid", "This deposit was already captured."),
    "ALREADY_AUTHORIZED": ("Deposit hold already placed", "This deposit hold was already authorized."),
    "CANCELLED": ("Checkout cancelled", "No payment was taken. The crawler cannot start a paid session."),
}


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _callback_page(result: PayPalApprovalCallbackResponse) -> HTMLResponse:
    """Small page for the buyer's browser after PayPal redirects back."""
    title, message = _CALLBACK_MESSAGES[result.outcome]
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · CrawlerTrust</title>
<style>
:root {{ color-scheme: light dark; --page: #f9f9f7; --text: #0b0b0b; --muted: #52514e; --card: #fcfcfb; --border: rgba(11,11,11,.1); }}
@media (prefers-color-scheme: dark) {{ :root {{ --page: #0d0d0d; --text: #f0efec; --muted: #c3c2b7; --card: #1a1a19; --border: rgba(255,255,255,.1); }} }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--page); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 16px; box-sizing: border-box; }}
main {{ max-width: 420px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; }}
h1 {{ font-size: 20px; margin: 0 0 8px; }} p {{ margin: 0 0 12px; color: var(--muted); }} code {{ font-size: 13px; }}
</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p>{html.escape(message)}</p>
<p>Deposit <code>{html.escape(result.deposit_id)}</code> · PayPal order <code>{html.escape(result.provider_order_id)}</code></p>
<p>You can close this window and return to the demo.</p>
</main>
<script>try {{ if (window.opener) {{ window.opener.postMessage({{ type: "crawlertrust-paypal", outcome: {result.outcome!r} }}, window.location.origin); setTimeout(() => window.close(), 1500); }} }} catch (_) {{}}</script>
</body></html>"""
    return HTMLResponse(body)


async def _authorize_deposit_access(
    *,
    deposit: DepositRecord,
    container: AppContainer,
    bearer_token: Optional[str],
    x_admin_token: Optional[str],
    claimed_crawler_id: Optional[str],
    claimed_challenge_id: Optional[str],
) -> None:
    """
    Verify that the caller is authorized to inspect or operate on `deposit`:
    1. Valid configured X-Admin-Token, OR
    2. Valid Bearer token bound to this deposit's session, OR
    3. Pre-session proof: matching crawler_id AND matching challenge_id.
    """
    if is_valid_admin_token(container, x_admin_token):
        return

    if bearer_token is not None:
        session = await container.session_manager.authenticate_token(bearer_token)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "invalid_credentials",
                    "message": "Invalid crawler bearer token.",
                },
            )
        if session.deposit_id != deposit.deposit_id or session.crawler_id != deposit.crawler_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden_deposit_access",
                    "message": "Bearer token belongs to a different crawler session/deposit.",
                },
            )
        return

    if not claimed_crawler_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_authorization",
                "message": (
                    "Deposit access requires Authorization: Bearer <token>, "
                    "X-Admin-Token, or matching crawler_id and challenge_id."
                ),
            },
        )

    if claimed_crawler_id != deposit.crawler_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden_deposit_access",
                "message": "Deposit belongs to a different crawler_id.",
            },
        )

    if claimed_challenge_id is not None and claimed_challenge_id != deposit.challenge_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden_deposit_access",
                "message": "Provided challenge_id does not match this deposit.",
            },
        )


@router.get(
    "/paypal/return",
    response_model=PayPalApprovalCallbackResponse,
    summary="Capture and verify an order after the buyer returns from PayPal approval",
)
async def paypal_approval_return(
    deposit_id: str,
    token: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Union[PayPalApprovalCallbackResponse, HTMLResponse]:
    deposit = await _get_paypal_callback_deposit(
        deposit_id=deposit_id,
        order_id=token,
        container=container,
    )
    try:
        updated = await container.deposit_manager.capture_deposit(
            deposit_id=deposit_id,
            crawler_id=deposit.crawler_id,
        )
    except PaymentApprovalRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "payer_approval_required", "message": str(exc)},
        ) from exc
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_state_transition", "message": str(exc)},
        ) from exc
    except PaymentProviderTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "payment_provider_timeout", "message": str(exc)},
        ) from exc
    except PaymentProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "payment_provider_error", "message": str(exc)},
        ) from exc

    if not updated.is_funded:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "capture_verification_failed",
                "message": updated.failure_reason or "PayPal payment could not be verified.",
            },
        )
    result = _paypal_callback_response(
        updated,
        "AUTHORIZED" if updated.payment_status == PaymentStatus.AUTHORIZED else "CAPTURED",
    )
    return _callback_page(result) if _wants_html(request) else result


@router.get(
    "/paypal/cancel",
    response_model=PayPalApprovalCallbackResponse,
    summary="Handle buyer cancellation without capturing or changing deposit payment status",
)
async def paypal_approval_cancel(
    deposit_id: str,
    token: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> Union[PayPalApprovalCallbackResponse, HTMLResponse]:
    deposit = await _get_paypal_callback_deposit(
        deposit_id=deposit_id,
        order_id=token,
        container=container,
    )
    if deposit.payment_status == PaymentStatus.CAPTURED:
        outcome: Literal["CANCELLED", "ALREADY_CAPTURED", "ALREADY_AUTHORIZED"] = "ALREADY_CAPTURED"
    elif deposit.payment_status == PaymentStatus.AUTHORIZED:
        outcome = "ALREADY_AUTHORIZED"
    else:
        outcome = "CANCELLED"
    result = _paypal_callback_response(deposit, outcome)
    return _callback_page(result) if _wants_html(request) else result


@router.post(
    "/create",
    response_model=DepositResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pending deposit for a 402 challenge and initiate the payment order",
)
async def create_deposit(
    payload: DepositCreateRequest,
    container: AppContainer = Depends(get_container),
) -> DepositResponse:
    try:
        record = await container.deposit_manager.create_deposit(
            challenge_id=payload.challenge_id,
            crawler_id=payload.crawler_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "challenge_not_found", "message": str(exc)},
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "challenge_ownership_mismatch", "message": str(exc)},
        ) from exc
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "challenge_already_used", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_challenge", "message": str(exc)},
        ) from exc
    except PaymentProviderTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "payment_provider_timeout", "message": str(exc)},
        ) from exc
    except PaymentProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "payment_provider_error", "message": str(exc)},
        ) from exc

    return container.deposit_manager.to_response(record)


@router.get(
    "/{deposit_id}",
    response_model=DepositResponse,
    summary="Inspect deposit status (requires owner authentication or admin token)",
)
async def get_deposit(
    deposit_id: str,
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
    x_crawler_id: Optional[str] = Header(default=None),
    x_challenge_id: Optional[str] = Header(default=None),
) -> DepositResponse:
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )

    # Require challenge_id proof if authenticating via X-Crawler-Id without bearer/admin token
    if bearer_token is None and not is_valid_admin_token(container, x_admin_token):
        if not x_crawler_id or not x_challenge_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "missing_authorization",
                    "message": (
                        "Provide Authorization: Bearer <token>, or both "
                        "X-Crawler-Id and X-Challenge-Id headers."
                    ),
                },
            )

    await _authorize_deposit_access(
        deposit=deposit,
        container=container,
        bearer_token=bearer_token,
        x_admin_token=x_admin_token,
        claimed_crawler_id=x_crawler_id,
        claimed_challenge_id=x_challenge_id,
    )
    return container.deposit_manager.to_response(deposit)


@router.get(
    "/{deposit_id}/explanation",
    response_model=DepositExplanationResponse,
    summary="Explain a deposit's refund or forfeiture decision, citing audit-log evidence",
)
async def explain_deposit(
    deposit_id: str,
    use_ai: bool = Query(
        default=True,
        description="Use the configured AI model when available; false returns the rule-based explanation.",
    ),
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
) -> DepositExplanationResponse:
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )

    is_admin = is_valid_admin_token(container, x_admin_token)
    if bearer_token is None and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_authorization",
                "message": "Provide the crawler's Authorization: Bearer <token>, or X-Admin-Token.",
            },
        )
    await _authorize_deposit_access(
        deposit=deposit,
        container=container,
        bearer_token=None if is_admin else bearer_token,
        x_admin_token=x_admin_token,
        claimed_crawler_id=None,
        claimed_challenge_id=None,
    )

    return await build_deposit_explanation(
        container, deposit, use_ai=use_ai, privileged=is_admin
    )


async def build_deposit_explanation(
    container: AppContainer,
    deposit: DepositRecord,
    *,
    use_ai: bool,
    privileged: bool,
) -> DepositExplanationResponse:
    """
    Explain a deposit from its audit evidence. Unprivileged callers can only trigger an
    AI explanation once the outcome is final, so they cannot run up model usage by
    interleaving requests and explanation calls; results are cached per deposit state.
    """
    ai_allowed = use_ai and (privileged or deposit.deposit_state in FINAL_DEPOSIT_STATES)
    skip_reason: Optional[str] = None
    if use_ai and not ai_allowed:
        skip_reason = (
            "AI explanations are generated once the deposit outcome is final; "
            "showing a rule-based explanation."
        )
    session = await container.session_manager.get_session_by_deposit(deposit.deposit_id)
    events = await container.audit_logger.get_events(deposit_id=deposit.deposit_id, limit=0)
    return await container.explainer.explain(
        deposit=deposit,
        session=session,
        events=events,
        use_ai=ai_allowed,
        skip_reason=skip_reason,
    )


@router.post(
    "/{deposit_id}/reconcile-refund",
    response_model=DepositResponse,
    summary="Reconcile a persisted PayPal refund without submitting another refund",
)
async def reconcile_refund(
    deposit_id: str,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> DepositResponse:
    if not is_valid_admin_token(container, x_admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_admin_token",
                "message": "A valid configured X-Admin-Token is required.",
            },
        )

    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": "Unknown deposit_id."},
        )
    if deposit.payment_mode != PaymentMode.PAYPAL_SANDBOX:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "unsupported_payment_mode",
                "message": "Refund reconciliation is only available for PayPal Sandbox deposits.",
            },
        )

    try:
        reconciled = await container.deposit_manager.reconcile_refund(deposit_id)
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "refund_reconciliation_conflict", "message": str(exc)},
        ) from exc

    return container.deposit_manager.to_response(reconciled)


@router.post(
    "/{deposit_id}/capture",
    response_model=DepositResponse,
    summary="Capture an approved payment order and verify the captured amount server-side",
)
async def capture_deposit(
    deposit_id: str,
    payload: Optional[DepositActionRequest] = Body(default=None),
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
    x_crawler_id: Optional[str] = Header(default=None),
    x_challenge_id: Optional[str] = Header(default=None),
) -> DepositResponse:
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )

    claimed_crawler = (payload.crawler_id if payload else None) or x_crawler_id
    claimed_challenge = (payload.challenge_id if payload else None) or x_challenge_id

    await _authorize_deposit_access(
        deposit=deposit,
        container=container,
        bearer_token=bearer_token,
        x_admin_token=x_admin_token,
        claimed_crawler_id=claimed_crawler,
        claimed_challenge_id=claimed_challenge,
    )

    try:
        updated = await container.deposit_manager.capture_deposit(
            deposit_id=deposit_id,
            crawler_id=deposit.crawler_id,
        )
    except PaymentApprovalRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "payer_approval_required",
                "message": str(exc),
                "approval_url": exc.approval_url,
            },
        ) from exc
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_state_transition", "message": str(exc)},
        ) from exc
    except PaymentProviderTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "payment_provider_timeout", "message": str(exc)},
        ) from exc
    except PaymentProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "payment_provider_error", "message": str(exc)},
        ) from exc

    if not updated.is_funded and updated.payment_status != PaymentStatus.VOIDED:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "capture_verification_failed",
                "message": updated.failure_reason or "Payment capture could not be verified.",
                "deposit": container.deposit_manager.to_response(updated).model_dump(mode="json"),
            },
        )

    return container.deposit_manager.to_response(updated)


@router.post(
    "/{deposit_id}/verify",
    response_model=DepositResponse,
    summary="Perform authoritative server-side verification of a deposit order with the payment provider",
)
async def verify_deposit(
    deposit_id: str,
    payload: Optional[DepositActionRequest] = Body(default=None),
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
    x_crawler_id: Optional[str] = Header(default=None),
    x_challenge_id: Optional[str] = Header(default=None),
) -> DepositResponse:
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )

    claimed_crawler = (payload.crawler_id if payload else None) or x_crawler_id
    claimed_challenge = (payload.challenge_id if payload else None) or x_challenge_id

    await _authorize_deposit_access(
        deposit=deposit,
        container=container,
        bearer_token=bearer_token,
        x_admin_token=x_admin_token,
        claimed_crawler_id=claimed_crawler,
        claimed_challenge_id=claimed_challenge,
    )

    try:
        updated = await container.deposit_manager.verify_deposit(
            deposit_id=deposit_id,
            crawler_id=deposit.crawler_id,
        )
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_state_transition", "message": str(exc)},
        ) from exc
    except PaymentProviderTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "payment_provider_timeout", "message": str(exc)},
        ) from exc
    except PaymentProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "payment_provider_error", "message": str(exc)},
        ) from exc

    if not updated.is_funded and updated.payment_status != PaymentStatus.VOIDED:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "payment_not_verified",
                "message": updated.failure_reason or "Payment is not captured/verified.",
                "deposit": container.deposit_manager.to_response(updated).model_dump(mode="json"),
            },
        )

    return container.deposit_manager.to_response(updated)


@router.post(
    "/{deposit_id}/refund",
    response_model=DepositResponse,
    summary="Evaluate refund eligibility from authoritative session history and execute refund if eligible",
)
async def refund_deposit(
    deposit_id: str,
    payload: Optional[DepositActionRequest] = Body(default=None),
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
    x_crawler_id: Optional[str] = Header(default=None),
    x_challenge_id: Optional[str] = Header(default=None),
) -> DepositResponse:
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )

    claimed_crawler = (payload.crawler_id if payload else None) or x_crawler_id
    claimed_challenge = (payload.challenge_id if payload else None) or x_challenge_id

    # If authenticating without bearer/admin token, require both crawler_id and challenge_id
    if bearer_token is None and not is_valid_admin_token(container, x_admin_token):
        if not claimed_crawler or not claimed_challenge:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "missing_authorization",
                    "message": (
                        "Refund request requires Authorization: Bearer <token>, "
                        "X-Admin-Token, or both crawler_id and challenge_id."
                    ),
                },
            )

    await _authorize_deposit_access(
        deposit=deposit,
        container=container,
        bearer_token=bearer_token,
        x_admin_token=x_admin_token,
        claimed_crawler_id=claimed_crawler,
        claimed_challenge_id=claimed_challenge,
    )

    session = await container.session_manager.get_session_by_deposit(deposit_id)
    try:
        updated_deposit, _, _ = await container.deposit_manager.evaluate_and_process_refund(
            deposit_id=deposit_id,
            crawler_id=deposit.crawler_id,
            session=session,
        )
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_refund_state", "message": str(exc)},
        ) from exc

    return container.deposit_manager.to_response(updated_deposit)
