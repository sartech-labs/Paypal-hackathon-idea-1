from __future__ import annotations

from typing import Any

import httpx
import pytest

from client import compliant


DEPOSIT_ID = "deposit-cli-refresh-123"
SESSION_TOKEN = "test-session-token-cli"


def _deposit(deposit_state: str, refund_status: str) -> dict[str, Any]:
    return {
        "deposit_id": DEPOSIT_ID,
        "payment_mode": "paypal_sandbox",
        "simulated": False,
        "deposit_state": deposit_state,
        "payment_status": "CAPTURED",
        "policy_decision": "ELIGIBLE_FOR_REFUND",
        "refund_status": refund_status,
        "provider_refund_id": "test-provider-refund-id",
    }


def _session() -> dict[str, Any]:
    return {
        "session_id": "session-cli-refresh-123",
        "status": "COMPLETED",
        "total_requests": 1,
        "allowed_requests": 1,
        "rejected_requests": 0,
        "total_violations": 0,
        "disallowed_path_violations": 0,
        "rate_limit_violations": 0,
        "budget_violations": 0,
        "disqualified": False,
        "disqualification_reasons": [],
    }


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    completion_deposit: dict[str, Any],
    refreshed_deposit: dict[str, Any] | None = None,
    refresh_status_code: int = 200,
) -> tuple[int, list[dict[str, str]]]:
    requests: list[dict[str, str]] = []
    real_httpx_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorization": request.headers.get("authorization", ""),
            }
        )
        path = request.url.path
        if path == "/gateway/news":
            if request.headers.get("authorization"):
                return httpx.Response(
                    200,
                    text="news",
                    headers={
                        "X-CrawlerTrust-Remaining-Budget": "9",
                        "X-CrawlerTrust-Remaining-Window": "2",
                    },
                )
            return httpx.Response(
                402,
                json={
                    "challenge_id": "challenge-cli-refresh-123",
                    "deposit": {"amount": "0.01", "currency": "USD"},
                    "policy_version": "test-policy",
                    "notice": "Payment required",
                },
            )
        if path == "/policies":
            return httpx.Response(
                200,
                json={
                    "site_name": "Test Site",
                    "site_id": "test-site",
                    "allowed_paths": ["/news"],
                    "disallowed_paths": [],
                    "rate_limit_requests": 3,
                    "rate_limit_window_seconds": 10,
                    "max_requests_per_session": 10,
                },
            )
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *")
        if path == "/deposits/create":
            return httpx.Response(
                201,
                json={
                    **_deposit("PENDING", "NOT_REQUESTED"),
                    "provider_order_id": "test-order-id",
                    "provider_order_status": "COMPLETED",
                },
            )
        if path == f"/deposits/{DEPOSIT_ID}/capture":
            return httpx.Response(
                200,
                json={
                    **_deposit("CAPTURED", "NOT_REQUESTED"),
                    "provider_capture_id": "test-capture-id",
                    "captured_amount": "0.01",
                    "currency": "USD",
                },
            )
        if path == f"/deposits/{DEPOSIT_ID}/verify":
            return httpx.Response(
                200,
                json={**_deposit("CAPTURED", "NOT_REQUESTED")},
            )
        if path == "/sessions/accept-policy":
            return httpx.Response(
                201,
                json={
                    "access_token": SESSION_TOKEN,
                    "session_id": "session-cli-refresh-123",
                    "policy_accepted_at": "2026-01-01T00:00:00Z",
                },
            )
        if path == "/sessions/complete":
            return httpx.Response(
                200,
                json={
                    "session": _session(),
                    "deposit": completion_deposit,
                    "refund_eligible": True,
                    "decision_reason": "Eligible",
                },
            )
        if path == f"/deposits/{DEPOSIT_ID}":
            if refresh_status_code != 200:
                return httpx.Response(refresh_status_code, json={"detail": "Unavailable"})
            return httpx.Response(200, json=refreshed_deposit)
        if path == "/audit/summary":
            return httpx.Response(
                200,
                json={
                    "total_events": 1,
                    "total_gateway_requests": 1,
                    "policy_violations": 0,
                    "violation_counts_by_reason": {},
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    def mocked_client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_httpx_client(*args, **kwargs)

    monkeypatch.setattr(compliant.httpx, "Client", mocked_client_factory)
    exit_code = compliant.run_compliant_crawler(
        crawler_id="crawler-cli-refresh",
        max_requests=1,
        payment_mode="paypal_sandbox",
        non_interactive=True,
    )
    return exit_code, requests


@pytest.mark.parametrize(
    "completion_state, completion_refund_state, refreshed_state, refreshed_refund_state",
    [
        ("REFUND_PENDING", "REFUND_PENDING", "REFUNDED", "REFUNDED"),
        ("REFUNDED", "REFUNDED", "REFUNDED", "REFUNDED"),
    ],
)
def test_compliant_cli_displays_latest_refreshed_refund_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    completion_state: str,
    completion_refund_state: str,
    refreshed_state: str,
    refreshed_refund_state: str,
) -> None:
    exit_code, requests = _run_cli(
        monkeypatch,
        completion_deposit=_deposit(completion_state, completion_refund_state),
        refreshed_deposit=_deposit(refreshed_state, refreshed_refund_state),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Composite Deposit State : REFUNDED" in output
    assert "Refund Transaction      : REFUNDED" in output
    assert "Payment Status          : CAPTURED" in output
    deposit_get = next(
        request for request in requests
        if request["method"] == "GET" and request["path"] == f"/deposits/{DEPOSIT_ID}"
    )
    assert deposit_get["authorization"] == f"Bearer {SESSION_TOKEN}"


def test_compliant_cli_falls_back_to_completion_state_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, requests = _run_cli(
        monkeypatch,
        completion_deposit=_deposit("REFUND_PENDING", "REFUND_PENDING"),
        refresh_status_code=503,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Could not refresh the latest deposit state" in output
    assert "displaying the session-completion response" in output
    assert "Composite Deposit State : REFUND_PENDING" in output
    assert "Refund Transaction      : REFUND_PENDING" in output
    assert "[SUCCESS]" not in output
    assert any(
        request["method"] == "GET"
        and request["path"] == f"/deposits/{DEPOSIT_ID}"
        and request["authorization"] == f"Bearer {SESSION_TOKEN}"
        for request in requests
    )
