"""Protected crawler gateway endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from server.core.agent_identity import verify_request
from server.dependencies import AppContainer, get_container
from server.schemas import SessionStatus
from server.services.origin_proxy import OriginError

router = APIRouter(tags=["Gateway"])


def _raw_request_path(request: Request) -> str:
    """The un-normalized URI path from the ASGI scope, without the query string."""
    raw_bytes = request.scope.get("raw_path")
    if isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
        raw_str = raw_bytes.decode("latin-1", errors="replace")
        return raw_str.split("?", 1)[0] or "/"
    return request.url.path or "/"


def _extract_raw_gateway_subpath(request: Request, fallback_path: str) -> str:
    """
    Extract the un-normalized raw URI path after `/gateway` from the ASGI scope
    so encoded path traversal attempts (e.g. `%2e%2e`, `%2f`, `%5c`) are caught.
    """
    raw_str = _raw_request_path(request)
    if raw_str == "/gateway" or raw_str == "/gateway/":
        return "/"
    if raw_str.startswith("/gateway/"):
        return "/" + raw_str[len("/gateway/") :]
    return raw_str or ("/" + fallback_path.lstrip("/") if fallback_path else "/")


def _request_details(request: Request) -> dict[str, str]:
    """Audit fields shared by every gateway event for a request."""
    run_id = request.headers.get("x-sara-playground-run")
    return {"playground_run_id": run_id} if run_id else {}


async def _handle_gateway_request(
    request: Request,
    path: str,
    container: AppContainer,
    authorization: Optional[str],
    x_crawler_id: Optional[str],
    crawler_id_query: Optional[str],
    raw_subpath: Optional[str] = None,
) -> Response:
    if raw_subpath is None:
        raw_subpath = _extract_raw_gateway_subpath(request, path)

    # 1. No Authorization header -> Issue HTTP 402 Payment Required challenge
    if authorization is None:
        settings = container.settings
        identity = verify_request(
            request.headers, request.method, request.headers.get("host", ""), request.url.path, container.agent_directory
        )
        identity_error = None
        if identity.signed and not identity.verified:
            identity_error = ("INVALID_SIGNATURE", identity.error or "The request signature did not verify.")
        elif not identity.verified and settings.require_signed_agents:
            identity_error = ("SIGNATURE_REQUIRED", "This site only admits agents that sign their requests (Web Bot Auth).")
        if identity_error:
            await container.audit_logger.log_event(
                event_type="AGENT_IDENTITY_REJECTED", crawler_id=x_crawler_id or crawler_id_query, path=raw_subpath,
                method=request.method, allowed=False, reason_code=identity_error[0],
                policy_version=container.site_config.policy_version, payment_mode=container.payment_provider.mode.value,
                details={**_request_details(request), "keyid": identity.keyid, "http_status": 401},
            )
            return JSONResponse(status_code=401, content={"error": "invalid_agent_identity", "reason_code": identity_error[0], "message": identity_error[1]})

        multiplier = None
        identity_info = identity.public()
        identity_info["standing"] = "unverified"
        candidate_crawler_id = identity.crawler_id or x_crawler_id or crawler_id_query
        if identity.verified:
            record = await container.deposit_manager.agent_record(identity.crawler_id or "")
            identity_info["record"] = record
            if record["forfeited"] >= settings.refuse_after_forfeits:
                await container.audit_logger.log_event(
                    event_type="AGENT_REFUSED", crawler_id=identity.crawler_id, path=raw_subpath, method=request.method,
                    allowed=False, reason_code="AGENT_REFUSED", policy_version=container.site_config.policy_version,
                    payment_mode=container.payment_provider.mode.value,
                    details={**_request_details(request), **record, "http_status": 403},
                )
                return JSONResponse(status_code=403, content={
                    "error": "agent_refused", "reason_code": "AGENT_REFUSED",
                    "message": f"This agent has had {record['forfeited']} deposits kept for breaking the rules and is no longer admitted.",
                })
            if record["forfeited"]:
                identity_info["standing"], multiplier = "flagged", settings.flagged_agent_multiplier
            elif record["clean"]:
                identity_info["standing"], multiplier = "trusted", Decimal("1") - settings.trusted_agent_discount
            else:
                identity_info["standing"] = "verified"
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
            multiplier=multiplier,
            identity=identity_info,
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
                **_request_details(request),
                "required_amount": challenge_record.required_amount,
                "currency": challenge_record.currency,
                "pricing_tier": challenge_record.pricing_tier,
                "http_status": 402,
                "agent_standing": identity_info["standing"],
                "verified_agent": identity_info.get("agent"),
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

    # Path shown in session history, audit events, and error responses: the
    # crawler's original request when the normalized form cannot represent it.
    display_path = decision.requested_path or decision.normalized_path or raw_subpath
    resource_tier = (
        container.pricing.tier_for_normalized_path(decision.normalized_path)
        if decision.requested_path is None
        else None
    )

    # 4. Record evaluation against session history when session is active or completed
    updated_session = session
    if session.status in (SessionStatus.ACTIVE, SessionStatus.COMPLETED):
        updated_session = await container.session_manager.record_gateway_evaluation(
            session_id=session.session_id,
            path=display_path,
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
        path=display_path,
        method=request.method,
        allowed=decision.allowed,
        reason_code=decision.reason_code,
        policy_version=decision.policy_version,
        payment_mode=session.payment_mode.value,
        details={
            **_request_details(request),
            "http_status": decision.http_status,
            "violation": decision.violation,
            "disqualifying": decision.disqualifying,
            "session_disqualified": updated_session.disqualified,
            "remaining_session_budget": decision.remaining_session_budget,
            "remaining_window_requests": decision.remaining_window_requests,
            "pricing_tier": resource_tier.tier_id if resource_tier else None,
            "deposit_pricing_tier": deposit.pricing_tier if deposit else None,
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
                "path": display_path,
                "remaining_session_budget": decision.remaining_session_budget,
                "remaining_window_requests": decision.remaining_window_requests,
                "retry_after_seconds": decision.retry_after_seconds,
            },
        )

    sara_headers = {
        "X-SARA-Session-Id": session.session_id,
        "X-SARA-Policy-Version": decision.policy_version,
        "X-SARA-Remaining-Budget": str(decision.remaining_session_budget),
        "X-SARA-Remaining-Window": str(decision.remaining_window_requests),
        **({"X-SARA-Pricing-Tier": resource_tier.tier_id} if resource_tier else {}),
    }

    # 7a. Reverse-proxy mode: forward the allowed request to the origin website.
    if container.origin_proxy is not None:
        body = b""
        if request.method not in ("GET", "HEAD"):
            body = await request.body()
        forward_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            in (
                "accept",
                "accept-language",
                "content-type",
                "if-match",
                "if-modified-since",
                "if-none-match",
                "range",
            )
        }
        try:
            origin = await container.origin_proxy.fetch(
                decision.normalized_path,
                request.url.query,
                method=request.method,
                body=body,
                headers=forward_headers,
            )
        except OriginError as exc:
            return JSONResponse(status_code=502, content={"error": "origin_unavailable", "message": str(exc)})
        return Response(
            content=origin.body,
            status_code=origin.status_code,
            media_type=origin.content_type,
            headers=origin.headers.with_extra(sara_headers),
        )

    # 7b. Serve authorized content from ContentService
    page = container.content_service.get_authorized_content(decision)
    return Response(
        content=page.body,
        status_code=200,
        media_type=page.content_type,
        headers=sara_headers,
    )


async def handle_agent_request(
    request: Request,
    path: str,
    container: AppContainer,
    raw_subpath: Optional[str] = None,
) -> Response:
    """Gate one transparent-edge agent request (the site root, not the legacy prefix)."""
    return await _handle_gateway_request(
        request=request,
        path=path,
        container=container,
        authorization=request.headers.get("authorization"),
        x_crawler_id=request.headers.get("x-crawler-id"),
        crawler_id_query=request.query_params.get("crawler_id"),
        raw_subpath=raw_subpath,
    )


@router.get("/gateway", summary="Access root protected content through the SARA gateway")
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
    summary="Access protected website paths through the SARA gateway",
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

