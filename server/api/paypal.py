"""PayPal assistant endpoints: read PayPal's records through PayPal's MCP server."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status

from server.dependencies import AppContainer, demo_access, get_container
from server.schemas import (
    PayPalAssistantRequest,
    PayPalAssistantResponse,
    PayPalAssistantStatus,
    PayPalOrderVerificationResponse,
    PayPalVerificationResponse,
)
from server.services.ai_client import AIRefusalError, AIServiceError, AIUnavailableError
from server.services.paypal_assistant import PayPalAssistantError
from server.services.paypal_mcp import READ_ONLY_TOOLS, PayPalMCPError, PayPalMCPUnavailableError

router = APIRouter(prefix="/paypal", tags=["PayPal assistant (MCP)"])


def _require_access(container: AppContainer, x_admin_token: Optional[str]) -> str:
    access = demo_access(container, x_admin_token)
    if access == "none":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "paypal_assistant_authorization_required",
                "message": "Provide a valid X-Admin-Token, or enable DEMO_PUBLIC=true for a public demo.",
            },
        )
    return access


def _spend_quota(container: AppContainer, access: str) -> None:
    if access == "public" and not container.paypal_quota.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "paypal_assistant_quota_exceeded",
                "message": "The hourly limit for public PayPal assistant requests was reached. Try again later.",
            },
        )


def _mcp_error(exc: PayPalMCPError) -> HTTPException:
    if isinstance(exc, PayPalMCPUnavailableError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "paypal_mcp_unavailable", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "paypal_mcp_error", "message": str(exc)},
    )


@router.get(
    "/assistant/status",
    response_model=PayPalAssistantStatus,
    summary="Whether the PayPal MCP server and an AI model are configured",
)
async def assistant_status(
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PayPalAssistantStatus:
    access = _require_access(container, x_admin_token)
    mcp = container.paypal_mcp
    return PayPalAssistantStatus(
        mcp_configured=mcp.is_configured(),
        mcp_unavailable_reason=mcp.unavailable_reason() or mcp.last_error,
        mcp_transport=mcp.transport,  # type: ignore[arg-type]
        mcp_server=mcp.server_label,
        read_only_tools=sorted(READ_ONLY_TOOLS),
        ai_available=container.ai_generator.is_available(),
        ai_provider=container.ai_generator.provider,
        ai_model=container.ai_generator.model,
        payment_mode=container.payment_provider.mode,
        access=access,  # type: ignore[arg-type]
        questions_remaining_this_hour=container.paypal_quota.remaining() if access == "public" else None,
    )


@router.post(
    "/orders/{order_id}/verify",
    response_model=PayPalOrderVerificationResponse,
    summary="Compare a commerce order (pay-per-access or products) with PayPal's records, through PayPal's MCP server",
)
async def verify_order(
    order_id: str,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PayPalOrderVerificationResponse:
    access = _require_access(container, x_admin_token)
    orders = {o.order_id: o for o in await container.commerce.list_orders()}
    order = orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "order_not_found", "message": f"Unknown order_id: {order_id}"})
    _spend_quota(container, access)
    try:
        return await container.paypal_assistant.verify_order(order)
    except PayPalAssistantError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "nothing_to_verify", "message": str(exc)}) from exc
    except PayPalMCPError as exc:
        raise _mcp_error(exc) from exc


@router.post(
    "/deposits/{deposit_id}/verify",
    response_model=PayPalVerificationResponse,
    summary="Compare a deposit with PayPal's records, read through PayPal's MCP server",
)
async def verify_deposit(
    deposit_id: str,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PayPalVerificationResponse:
    access = _require_access(container, x_admin_token)
    deposit = await container.deposit_manager.get_deposit(deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": f"Unknown deposit_id: {deposit_id}"},
        )
    _spend_quota(container, access)
    try:
        return await container.paypal_assistant.verify_deposit(deposit)
    except PayPalAssistantError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "nothing_to_verify", "message": str(exc)},
        ) from exc
    except PayPalMCPError as exc:
        raise _mcp_error(exc) from exc


@router.post(
    "/assistant/ask",
    response_model=PayPalAssistantResponse,
    summary="Ask the AI PayPal assistant (read-only PayPal MCP tools plus the gateway's records)",
)
async def ask_assistant(
    payload: PayPalAssistantRequest,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> PayPalAssistantResponse:
    access = _require_access(container, x_admin_token)
    if not container.ai_generator.is_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "ai_unavailable",
                "message": "No AI key is configured on the gateway. Set OPENAI_API_KEY or ANTHROPIC_API_KEY.",
            },
        )
    _spend_quota(container, access)
    try:
        return await container.paypal_assistant.ask(payload.question.strip())
    except PayPalMCPError as exc:
        raise _mcp_error(exc) from exc
    except AIUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "ai_unavailable", "message": str(exc)},
        ) from exc
    except AIRefusalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "ai_refused", "message": str(exc)},
        ) from exc
    except AIServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "ai_error", "message": str(exc)},
        ) from exc
