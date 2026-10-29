"""Session policy acceptance, status inspection, completion, and revocation endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status

from server.core.security import StateTransitionError, utc_now_iso
from server.dependencies import (
    AppContainer,
    extract_optional_bearer_token,
    get_container,
    is_valid_admin_token,
    require_authenticated_session,
)
from server.schemas import (
    PaymentStatus,
    PolicyAcceptRequest,
    SessionCompleteResponse,
    SessionIssueResponse,
    SessionRecord,
    SessionRevokeRequest,
    SessionStatusResponse,
)

router = APIRouter(prefix="/sessions", tags=["Sessions"])


@router.post(
    "/accept-policy",
    response_model=SessionIssueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Explicitly accept the current crawling policy and receive a one-time bearer token",
)
async def accept_policy(
    payload: PolicyAcceptRequest,
    container: AppContainer = Depends(get_container),
) -> SessionIssueResponse:
    if not payload.agree_to_terms:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "policy_not_accepted",
                "message": "Crawler must explicitly set agree_to_terms=true to accept the policy.",
            },
        )

    if payload.policy_version != container.site_config.policy_version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "policy_version_mismatch",
                "message": (
                    f"Expected policy_version '{container.site_config.policy_version}', "
                    f"received '{payload.policy_version}'."
                ),
            },
        )

    deposit = await container.deposit_manager.get_deposit(payload.deposit_id)
    if deposit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "deposit_not_found", "message": "Unknown deposit_id."},
        )

    if deposit.crawler_id != payload.crawler_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "deposit_ownership_mismatch",
                "message": "Deposit belongs to a different crawler_id.",
            },
        )

    if deposit.challenge_id != payload.challenge_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "challenge_mismatch",
                "message": "challenge_id does not match the challenge bound to this deposit.",
            },
        )

    if deposit.payment_status != PaymentStatus.CAPTURED or not deposit.provider_capture_id:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "payment_not_captured",
                "message": "Deposit must be captured and verified server-side before session issuance.",
            },
        )

    if deposit.policy_accepted or deposit.session_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "session_already_issued",
                "message": "Policy was already accepted and a session was already issued for this deposit.",
            },
        )

    accepted_at = utc_now_iso()
    try:
        session_record, issue_response = await container.session_manager.issue_session(
            crawler_id=payload.crawler_id,
            challenge_id=payload.challenge_id,
            deposit_id=payload.deposit_id,
            policy_version=payload.policy_version,
            policy_accepted_at=accepted_at,
            payment_reference=deposit.provider_capture_id,
            payment_mode=deposit.payment_mode,
            session_ttl_seconds=container.site_config.session_ttl_seconds,
        )

        await container.deposit_manager.record_policy_acceptance_and_bind_session(
            deposit_id=payload.deposit_id,
            challenge_id=payload.challenge_id,
            crawler_id=payload.crawler_id,
            policy_version=payload.policy_version,
            agree_to_terms=payload.agree_to_terms,
            session_id=session_record.session_id,
            accepted_at=accepted_at,
        )
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_state_transition", "message": str(exc)},
        ) from exc

    await container.audit_logger.log_event(
        event_type="POLICY_ACCEPTED",
        crawler_id=session_record.crawler_id,
        challenge_id=session_record.challenge_id,
        session_id=session_record.session_id,
        deposit_id=session_record.deposit_id,
        allowed=True,
        reason_code="POLICY_AGREEMENT_RECORDED",
        policy_version=session_record.policy_version,
        payment_mode=session_record.payment_mode.value,
        details={
            "payment_reference": session_record.payment_reference,
            "policy_accepted_at": accepted_at,
        },
    )

    await container.audit_logger.log_event(
        event_type="SESSION_ISSUED",
        crawler_id=session_record.crawler_id,
        challenge_id=session_record.challenge_id,
        session_id=session_record.session_id,
        deposit_id=session_record.deposit_id,
        allowed=True,
        reason_code="TOKEN_ISSUED",
        policy_version=session_record.policy_version,
        payment_mode=session_record.payment_mode.value,
        details={
            "expires_at": session_record.expires_at,
            "token_hash_prefix": session_record.token_hash[:12],
        },
    )

    return issue_response


@router.get(
    "/me",
    response_model=SessionStatusResponse,
    summary="Return the authenticated crawler's current session status and violation counters",
)
async def get_my_session(
    session: SessionRecord = Depends(require_authenticated_session),
    container: AppContainer = Depends(get_container),
) -> SessionStatusResponse:
    return container.session_manager.to_status_response(session)


@router.post(
    "/complete",
    response_model=SessionCompleteResponse,
    summary="Complete the authenticated crawler session and trigger authoritative refund evaluation",
)
async def complete_session(
    session: SessionRecord = Depends(require_authenticated_session),
    container: AppContainer = Depends(get_container),
) -> SessionCompleteResponse:
    try:
        completed_session = await container.session_manager.complete_session(
            session.session_id
        )
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_session_state", "message": str(exc)},
        ) from exc

    await container.audit_logger.log_event(
        event_type="SESSION_COMPLETED",
        crawler_id=completed_session.crawler_id,
        challenge_id=completed_session.challenge_id,
        session_id=completed_session.session_id,
        deposit_id=completed_session.deposit_id,
        allowed=True,
        reason_code="CRAWL_COMPLETED",
        policy_version=completed_session.policy_version,
        payment_mode=completed_session.payment_mode.value,
        details={
            "total_requests": completed_session.total_requests,
            "allowed_requests": completed_session.allowed_requests,
            "rejected_requests": completed_session.rejected_requests,
            "total_violations": completed_session.total_violations,
            "disqualified": completed_session.disqualified,
        },
    )

    try:
        updated_deposit, eligible, decision_reason = (
            await container.deposit_manager.evaluate_and_process_refund(
                deposit_id=completed_session.deposit_id,
                crawler_id=completed_session.crawler_id,
                session=completed_session,
            )
        )
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "refund_evaluation_conflict", "message": str(exc)},
        ) from exc

    return SessionCompleteResponse(
        session=container.session_manager.to_status_response(completed_session),
        deposit=container.deposit_manager.to_response(updated_deposit),
        refund_eligible=eligible,
        decision_reason=decision_reason,
    )


@router.post(
    "/revoke",
    response_model=SessionStatusResponse,
    summary="Revoke the authenticated crawler session or an administratively specified session",
)
async def revoke_session(
    payload: Optional[SessionRevokeRequest] = Body(default=None),
    container: AppContainer = Depends(get_container),
    bearer_token: Optional[str] = Depends(extract_optional_bearer_token),
    x_admin_token: Optional[str] = Header(default=None),
) -> SessionStatusResponse:
    is_admin = is_valid_admin_token(container, x_admin_token)
    target_session_id: Optional[str] = payload.session_id if payload else None
    reason = payload.reason if payload else "Revoked by request"

    if is_admin and target_session_id:
        target = await container.session_manager.get_session(target_session_id)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "session_not_found", "message": f"Unknown session_id: {target_session_id}"},
            )
    else:
        if not bearer_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "missing_credentials",
                    "message": "Provide Authorization: Bearer <token> or valid X-Admin-Token.",
                },
            )
        caller_session = await container.session_manager.authenticate_token(bearer_token)
        if caller_session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "invalid_credentials",
                    "message": "Invalid crawler bearer token.",
                },
            )
        if target_session_id and target_session_id != caller_session.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "forbidden_session_revocation",
                    "message": "Crawlers cannot revoke another crawler's session.",
                },
            )
        target = caller_session

    try:
        revoked = await container.session_manager.revoke_session(target.session_id)
    except StateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_session_state", "message": str(exc)},
        ) from exc

    await container.audit_logger.log_event(
        event_type="SESSION_REVOKED",
        crawler_id=revoked.crawler_id,
        challenge_id=revoked.challenge_id,
        session_id=revoked.session_id,
        deposit_id=revoked.deposit_id,
        allowed=True,
        reason_code="REVOKED",
        policy_version=revoked.policy_version,
        payment_mode=revoked.payment_mode.value,
        details={"reason": reason, "revoked_by_admin": is_admin},
    )

    return container.session_manager.to_status_response(revoked)

