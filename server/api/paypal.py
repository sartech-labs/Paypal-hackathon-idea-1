"""PayPal assistant endpoints: read PayPal's records through PayPal's MCP server."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from urllib.parse import urlencode

from server.api.dashboard import _dashboard_access
from server.dependencies import AppContainer, demo_access, get_container, is_valid_admin_token
from server.schemas import PaymentMode
from server.services.payment_service import PaymentProviderError
from server.services.paypal_login import PayPalLoginError
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
logger = logging.getLogger("sara.paypal")


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


@router.get("/account", summary="The PayPal account this gateway's deposits and sales are paid to")
async def paypal_account(
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> dict:
    """Payout account and settlement settings, as the owner's Payouts card shows them.

    The credentials are checked with PayPal on every call (the token is cached). The receiving
    business account is read from the payee of the latest real order, and remembered."""
    _dashboard_access(container, x_admin_token)
    provider = container.payment_provider
    mode = provider.mode.value
    settlement = container.deposit_manager.settlement_mode
    info: dict = {
        "payment_mode": mode,
        "environment": {"paypal_sandbox": "sandbox", "mock": "simulated"}.get(mode, "live"),
        "settlement_mode": getattr(settlement, "value", str(settlement)),
        "currency": container.site_config.currency,
        "autopay": provider.autopay,
    }
    cached = getattr(container, "paypal_identity", None)
    if cached and cached.get("merchant_email"):
        identity = dict(cached)
    else:
        order_id = None
        deposits = await container.deposit_manager.list_deposits()
        real = [d for d in deposits if d.provider_order_id and d.payment_mode == PaymentMode.PAYPAL_SANDBOX]
        if real:
            order_id = max(real, key=lambda d: d.created_at).provider_order_id
        try:
            identity = await provider.account_identity(order_id=order_id)
        except PaymentProviderError as exc:
            identity = {"connected": False, "error": str(exc)}
        container.paypal_identity = identity
    info["login_available"] = container.paypal_login.available
    info["owner_connection"] = container.paypal_login.load()
    if not identity.get("merchant_email") and container.settings.paypal_merchant_email:
        identity["merchant_email"] = container.settings.paypal_merchant_email
        identity["merchant_source"] = "configured"
    elif identity.get("merchant_email"):
        identity.setdefault("merchant_source", "paypal")
    info.update(identity)
    return info


# --- Log in with PayPal: the owner connects the account they are paid to -------------------

@router.post("/connect/start", summary="Start Log in with PayPal for the owner's payout account")
async def paypal_connect_start(
    request: Request,
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> dict:
    if not is_valid_admin_token(container, x_admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "owner_required", "message": "Sign in as the owner to connect PayPal."},
        )
    # After PayPal, send the owner back to the console they started from (same host).
    return_to = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}/console"
    try:
        url = container.paypal_login.authorize_url(return_to=return_to)
    except PayPalLoginError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "paypal_unavailable", "message": str(exc)}) from exc
    return {"authorize_url": url, "redirect_uri": container.paypal_login.redirect_uri}


@router.get("/connect/callback", include_in_schema=False)
async def paypal_connect_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    container: AppContainer = Depends(get_container),
) -> Response:
    """PayPal sends the owner back here. Only a state this gateway issued is accepted.

    The owner is then sent back to the console they started from, with the outcome."""
    valid, return_to = (False, None) if error else container.paypal_login.consume_state(state)
    if error:
        logger.info("Log in with PayPal returned an error: %s", error)
        return _connect_result(return_to, False, error_description or "PayPal sign-in was cancelled.")
    if not valid:
        logger.warning("Log in with PayPal callback refused: unknown or expired state")
        return _connect_result(
            None,
            False,
            "This sign-in started from an old or already used link. Go back to the console and "
            "click Log in with PayPal again.",
        )
    if not code:
        return _connect_result(return_to, False, "PayPal did not return an authorization code.")
    try:
        connection = await asyncio.to_thread(container.paypal_login.complete, code)
    except PayPalLoginError as exc:
        logger.warning("Log in with PayPal could not finish: %s", exc)
        return _connect_result(return_to, False, str(exc))
    label = connection.get("email") or "your PayPal account"
    return _connect_result(return_to, True, f"Payouts now go to {label}.")


def _connect_result(return_to: Optional[str], ok: bool, message: str) -> Response:
    """Back to the console with the outcome, or a small page when there is nowhere to go."""
    if return_to:
        query = urlencode({"paypal": "connected" if ok else "error", "paypal_message": message[:300]})
        return RedirectResponse(f"{return_to}?{query}#setup", status_code=status.HTTP_303_SEE_OTHER)
    return _connect_page(ok, message)


@router.post("/connect/disconnect", summary="Disconnect the owner's PayPal account")
async def paypal_connect_disconnect(
    container: AppContainer = Depends(get_container),
    x_admin_token: Optional[str] = Header(default=None),
) -> dict:
    if not is_valid_admin_token(container, x_admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "owner_required", "message": "Sign in as the owner to change PayPal."},
        )
    container.paypal_login.disconnect()
    return {"connected": False}


def _connect_page(ok: bool, message: str) -> HTMLResponse:
    title = "PayPal connected" if ok else "PayPal not connected"
    payload = json.dumps({"type": "sara-paypal-connect", "ok": ok})
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e8edf6;
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}main{{max-width:420px;padding:28px;text-align:center}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:#a9b4c8;margin:0}}</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><p>You can close this window.</p></main>
<script>try{{if(window.opener){{window.opener.postMessage({payload},"*");setTimeout(function(){{window.close();}},900);}}}}catch(e){{}}</script>
</body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)
