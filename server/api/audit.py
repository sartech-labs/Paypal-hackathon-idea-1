"""Protected audit event query and summary endpoints."""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from server.dependencies import (
    AppContainer,
    extract_optional_bearer_token,
    get_container,
    is_valid_admin_token,
)
from server.schemas import AuditEvent, AuditSummaryResponse, SessionRecord

router = APIRouter(prefix="/audit", tags=["Audit"])


async def _resolve_audit_access(
    container: AppContainer,
    bearer_token: Optional[str],
    x_admin_token: Optional[str],
) -> Tuple[bool, Optional[SessionRecord]]:
    """
    Authenticate audit access:
    - Returns `(True, None)` if authenticated with a valid configured `X-Admin-Token`.
    - Returns `(False, session)` if authenticated with a valid crawler session bearer token
      (restricting access strictly to that crawler session's own audit records).
    - Raises HTTP 401 if neither is provided or credentials are invalid.
    """
    if x_admin_token is not None:
        if is_valid_admin_token(container, x_admin_token):
            return True, None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_admin_token",
                "message": "Invalid or unconfigured X-Admin-Token.",
            },
        )

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
        return False, session

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "missing_audit_authorization",
            "message": (
                "Audit endpoints require X-Admin-Token (for global scope) or "
                "Authorization: Bearer <crawler_token> (for session-scoped access)."
            ),
        },
    )


@router.get(
    "/events",
    response_model=list[AuditEvent],
    summary="Retrieve filtered audit events (global for admin, or session-scoped for authenticated crawler)",
)
async def get_audit_events(
    crawler_id: Optional[str] = Query(default=None),
    session_id: Optional[str] = Query(default=None),
    deposit_id: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
) -> list[AuditEvent]:
    is_admin, caller_session = await _resolve_audit_access(
        container, bearer_token, x_admin_token
    )

    if not is_admin:
        assert caller_session is not None
        if crawler_id and crawler_id != caller_session.crawler_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden_audit_scope",
                    "message": "Crawlers cannot query audit events for another crawler_id.",
                },
            )
        if session_id and session_id != caller_session.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden_audit_scope",
                    "message": "Crawlers cannot query audit events for another session_id.",
                },
            )
        if deposit_id and deposit_id != caller_session.deposit_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden_audit_scope",
                    "message": "Crawlers cannot query audit events for another deposit_id.",
                },
            )
        crawler_id = caller_session.crawler_id
        session_id = caller_session.session_id

    return await container.audit_logger.get_events(
        crawler_id=crawler_id,
        session_id=session_id,
        deposit_id=deposit_id,
        event_type=event_type,
        limit=limit,
    )


@router.get(
    "/summary",
    response_model=AuditSummaryResponse,
    summary="Return aggregated request counts, violations, deposit states, and refund outcomes",
)
async def get_audit_summary(
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
) -> AuditSummaryResponse:
    is_admin, caller_session = await _resolve_audit_access(
        container, bearer_token, x_admin_token
    )
    deposits = await container.deposit_manager.list_deposits()
    sessions = await container.session_manager.list_sessions()

    if is_admin:
        return await container.audit_logger.build_summary(
            deposits=deposits,
            sessions=sessions,
            scope="global",
        )

    assert caller_session is not None
    return await container.audit_logger.build_summary(
        deposits=deposits,
        sessions=sessions,
        scope="session",
        scoped_session_id=caller_session.session_id,
        scoped_crawler_id=caller_session.crawler_id,
    )

