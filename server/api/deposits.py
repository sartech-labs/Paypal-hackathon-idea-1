"""Deposit creation, payment capture, verification, and refund endpoints."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status

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
    DepositRecord,
    DepositResponse,
    PaymentMode,
    PaymentStatus,
    PayPalApprovalCallbackResponse,
)
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
    outcome: Literal["CAPTURED", "CANCELLED", "ALREADY_CAPTURED"],
) -> PayPalApprovalCallbackResponse:
    return PayPalApprovalCallbackResponse(
        outcome=outcome,
        deposit_id=deposit.deposit_id,
        payment_status=deposit.payment_status,
        deposit_state=deposit.deposit_state,
        provider_order_id=deposit.provider_order_id or "",
        provider_capture_id=deposit.provider_capture_id,
        captured_amount=deposit.captured_amount,
        currency=deposit.currency,
    )


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
    container: AppContainer = Depends(get_container),
) -> PayPalApprovalCallbackResponse:
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

    if updated.payment_status != PaymentStatus.CAPTURED:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "capture_verification_failed",
                "message": updated.failure_reason or "PayPal capture could not be verified.",
            },
        )
    return _paypal_callback_response(updated, "CAPTURED")


@router.get(
    "/paypal/cancel",
    response_model=PayPalApprovalCallbackResponse,
    summary="Handle buyer cancellation without capturing or changing deposit payment status",
)
async def paypal_approval_cancel(
    deposit_id: str,
    token: str,
    container: AppContainer = Depends(get_container),
) -> PayPalApprovalCallbackResponse:
    deposit = await _get_paypal_callback_deposit(
        deposit_id=deposit_id,
        order_id=token,
        container=container,
    )
    outcome = (
        "ALREADY_CAPTURED"
        if deposit.payment_status == PaymentStatus.CAPTURED
        else "CANCELLED"
    )
    return _paypal_callback_response(deposit, outcome)


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

    if updated.payment_status != PaymentStatus.CAPTURED:
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

    if updated.payment_status != PaymentStatus.CAPTURED:
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
