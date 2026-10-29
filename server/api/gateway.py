"""Protected crawler gateway endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from server.dependencies import AppContainer, get_container
from server.schemas import SessionStatus

router = APIRouter(tags=["Gateway"])


def _extract_raw_gateway_subpath(request: Request, fallback_path: str) -> str:
    """
    Extract the un-normalized raw URI path after `/gateway` from the ASGI scope
    so encoded path traversal attempts (e.g. `%2e%2e`, `%2f`, `%5c`) are caught.
    """
    raw_bytes = request.scope.get("raw_path")
    if isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
        raw_str = raw_bytes.decode("latin-1", errors="replace")
        # Strip query string if present in raw_path
        raw_str = raw_str.split("?", 1)[0]
        if raw_str == "/gateway" or raw_str == "/gateway/":
            return "/"
        if raw_str.startswith("/gateway/"):
            return "/" + raw_str[len("/gateway/") :]
        return raw_str
    return "/" + fallback_path.lstrip("/") if fallback_path else "/"


async def _handle_gateway_request(
    request: Request,
    path: str,
    container: AppContainer,
    authorization: Optional[str],
    x_crawler_id: Optional[str],
    crawler_id_query: Optional[str],
) -> Response:
    raw_subpath = _extract_raw_gateway_subpath(request, path)

    # 1. No Authorization header -> Issue HTTP 402 Payment Required challenge
    if authorization is None:
        candidate_crawler_id = x_crawler_id or crawler_id_query
        try:
            resolved_crawler_id = container.identity_service.resolve_or_generate_crawler_id(
                candidate_crawler_id
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_crawler_id",
                    "message": str(exc),
                },
            )

        challenge_record, challenge_response = container.identity_service.build_challenge(
            crawler_id=resolved_crawler_id,
            requested_path=raw_subpath,
            challenge_ttl_seconds=container.site_config.challenge_ttl_seconds,
        )
        await container.deposit_manager.register_challenge(challenge_record)
        await container.audit_logger.log_event(
            event_type="CHALLENGE_ISSUED",
            crawler_id=resolved_crawler_id,
            challenge_id=challenge_record.challenge_id,
            path=raw_subpath,
            method=request.method,
            allowed=False,
            reason_code="PAYMENT_REQUIRED",
            policy_version=container.site_config.policy_version,
            payment_mode=container.payment_provider.mode.value,
            details={
                "required_amount": challenge_record.required_amount,
                "currency": challenge_record.currency,
            },
        )
        return JSONResponse(
            status_code=402,
            content=challenge_response.model_dump(mode="json"),
        )

    # 2. Validate Authorization header format
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        await container.audit_logger.log_event(
            event_type="GATEWAY_AUTH_FAILED",
            path=raw_subpath,
            method=request.method,
            allowed=False,
            reason_code="MALFORMED_AUTH_HEADER",
            policy_version=container.site_config.policy_version,
            payment_mode=container.payment_provider.mode.value,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": "invalid_credentials",
                "reason_code": "MALFORMED_AUTH_HEADER",
                "message": "Authorization header must be 'Bearer <crawler_token>'.",
            },
        )

    raw_token = parts[1].strip()
    session = await container.session_manager.authenticate_token(raw_token)
    if session is None:
        await container.audit_logger.log_event(
            event_type="GATEWAY_AUTH_FAILED",
            path=raw_subpath,
            method=request.method,
            allowed=False,
            reason_code="INVALID_SESSION",
            policy_version=container.site_config.policy_version,
            payment_mode=container.payment_provider.mode.value,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": "invalid_credentials",
                "reason_code": "INVALID_SESSION",
                "message": "Invalid or unknown crawler bearer token.",
            },
        )

    deposit = await container.deposit_manager.get_deposit(session.deposit_id)

    # 3. Evaluate crawling policy
    decision = container.policy_engine.evaluate_request(
        path_param=path,
        raw_request_path=raw_subpath,
        session=session,
        deposit=deposit,
    )

    # 4. Record evaluation against session history when session is active or completed
    updated_session = session
    if session.status in (SessionStatus.ACTIVE, SessionStatus.COMPLETED):
        updated_session = await container.session_manager.record_gateway_evaluation(
            session_id=session.session_id,
            path=decision.normalized_path if decision.normalized_path else raw_subpath,
            method=request.method,
            allowed=decision.allowed,
            reason_code=decision.reason_code,
            reason=decision.reason,
            violation=decision.violation,
            disqualifying_immediate=decision.disqualifying,
            refund_policy=container.site_config.refund_policy,
        )

    # 5. Record durable append-only audit event
    if decision.allowed:
        audit_type = "GATEWAY_ACCESS_ALLOWED"
    elif decision.violation:
        audit_type = "POLICY_VIOLATION"
    else:
        audit_type = "GATEWAY_ACCESS_REJECTED"

    await container.audit_logger.log_event(
        event_type=audit_type,
        crawler_id=session.crawler_id,
        challenge_id=session.challenge_id,
        session_id=session.session_id,
        deposit_id=session.deposit_id,
        path=decision.normalized_path if decision.normalized_path else raw_subpath,
        method=request.method,
        allowed=decision.allowed,
        reason_code=decision.reason_code,
        policy_version=decision.policy_version,
        payment_mode=session.payment_mode.value,
        details={
            "http_status": decision.http_status,
            "violation": decision.violation,
            "disqualifying": decision.disqualifying,
            "session_disqualified": updated_session.disqualified,
            "remaining_session_budget": decision.remaining_session_budget,
            "remaining_window_requests": decision.remaining_window_requests,
        },
    )

    # 6. Reject if not authorized (never touches or reads disallowed content)
    if not decision.allowed:
        headers: dict[str, str] = {}
        if decision.http_status == 429 and decision.retry_after_seconds > 0:
            headers["Retry-After"] = str(max(1, int(round(decision.retry_after_seconds))))
        return JSONResponse(
            status_code=decision.http_status,
            headers=headers,
            content={
                "error": decision.reason_code.lower(),
                "allowed": False,
                "reason_code": decision.reason_code,
                "reason": decision.reason,
                "violation": decision.violation,
                "disqualifying": decision.disqualifying,
                "session_disqualified": updated_session.disqualified,
                "policy_version": decision.policy_version,
                "path": decision.normalized_path,
                "remaining_session_budget": decision.remaining_session_budget,
                "remaining_window_requests": decision.remaining_window_requests,
                "retry_after_seconds": decision.retry_after_seconds,
            },
        )

    # 7. Serve authorized content from ContentService
    page = container.content_service.get_authorized_content(decision)
    return Response(
        content=page.body,
        status_code=200,
        media_type=page.content_type,
        headers={
            "X-CrawlerTrust-Session-Id": session.session_id,
            "X-CrawlerTrust-Policy-Version": decision.policy_version,
            "X-CrawlerTrust-Remaining-Budget": str(decision.remaining_session_budget),
            "X-CrawlerTrust-Remaining-Window": str(decision.remaining_window_requests),
        },
    )


@router.get("/gateway", summary="Access root protected content through the CrawlerTrust gateway")
async def gateway_root(
    request: Request,
    container: AppContainer = Depends(get_container),
    authorization: Optional[str] = Header(default=None),
    x_crawler_id: Optional[str] = Header(default=None),
    crawler_id: Optional[str] = Query(default=None),
) -> Response:
    return await _handle_gateway_request(
        request=request,
        path="/",
        container=container,
        authorization=authorization,
        x_crawler_id=x_crawler_id,
        crawler_id_query=crawler_id,
    )


@router.get(
    "/gateway/{path:path}",
    summary="Access protected website paths through the CrawlerTrust gateway",
)
async def gateway_path(
    path: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    authorization: Optional[str] = Header(default=None),
    x_crawler_id: Optional[str] = Header(default=None),
    crawler_id: Optional[str] = Query(default=None),
) -> Response:
    return await _handle_gateway_request(
        request=request,
        path=path,
        container=container,
        authorization=authorization,
        x_crawler_id=x_crawler_id,
        crawler_id_query=crawler_id,
    )

