"""Unit and integration tests for PayPalSandboxProvider using mocked HTTPX transports."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.schemas import DepositState, RefundTransactionStatus
from server.services.payment_service import PayPalSandboxProvider


def _build_paypal_mock_transport(
    *,
    captured_amount: str = "0.01",
    captured_currency: str = "USD",
    order_status_on_capture: str = "COMPLETED",
    capture_status: str = "COMPLETED",
    require_approval_on_capture: bool = False,
    refund_status: str = "COMPLETED",
    timeout_on_order: bool = False,
    timeout_on_refund: bool = False,
    malformed_refund_json: bool = False,
    refund_payload: dict[str, Any] | None = None,
    include_refunds_in_order_lookup: bool = True,
    token_status: int = 200,
    call_counters: dict[str, int] | None = None,
) -> httpx.MockTransport:
    counters = call_counters if call_counters is not None else {}
    existing_refunds: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counters[path] = counters.get(path, 0) + 1

        if path == "/v1/oauth2/token":
            return httpx.Response(
                token_status,
                json={"access_token": "sandbox_oauth_token_xyz", "expires_in": 3600},
            )

        if path == "/v2/checkout/orders" and request.method == "POST":
            if timeout_on_order:
                raise httpx.ReadTimeout("Simulated PayPal timeout", request=request)
            assert "PayPal-Request-Id" in request.headers
            return httpx.Response(
                201,
                json={
                    "id": "PAYPAL_ORD_1001",
                    "status": "CREATED",
                    "links": [
                        {
                            "href": "https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL_ORD_1001",
                            "rel": "approve",
                            "method": "GET",
                        }
                    ],
                },
            )

        if path == "/v2/checkout/orders/PAYPAL_ORD_1001/capture" and request.method == "POST":
            if require_approval_on_capture:
                return httpx.Response(
                    422,
                    json={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": "ORDER_NOT_APPROVED"}],
                    },
                )
            return httpx.Response(
                201,
                json={
                    "id": "PAYPAL_ORD_1001",
                    "status": order_status_on_capture,
                    "purchase_units": [
                        {
                            "payments": {
                                "refunds": existing_refunds,
                                "captures": [
                                    {
                                        "id": "PAYPAL_CAP_5001",
                                        "status": capture_status,
                                        "amount": {
                                            "value": captured_amount,
                                            "currency_code": captured_currency,
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                },
            )

        if path == "/v2/checkout/orders/PAYPAL_ORD_1001" and request.method == "GET":
            if require_approval_on_capture:
                return httpx.Response(
                    200,
                    json={
                        "id": "PAYPAL_ORD_1001",
                        "status": "CREATED",
                        "links": [
                            {
                                "href": "https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL_ORD_1001",
                                "rel": "approve",
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "PAYPAL_ORD_1001",
                    "status": order_status_on_capture,
                    "purchase_units": [
                        {
                            "payments": {
                                **(
                                    {"refunds": existing_refunds}
                                    if include_refunds_in_order_lookup
                                    else {}
                                ),
                                "captures": [
                                    {
                                        "id": "PAYPAL_CAP_5001",
                                        "status": capture_status,
                                        "amount": {
                                            "value": captured_amount,
                                            "currency_code": captured_currency,
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                },
            )

        if path == "/v2/payments/captures/PAYPAL_CAP_5001/refund" and request.method == "POST":
            assert "PayPal-Request-Id" in request.headers
            if timeout_on_refund:
                existing_refunds.append(
                    refund_payload
                    or {
                        "id": "PAYPAL_REF_9001",
                        "status": "COMPLETED",
                        "amount": {"value": captured_amount, "currency_code": captured_currency},
                    }
                )
                raise httpx.ReadTimeout("Simulated PayPal refund timeout", request=request)
            if refund_status == "HTTP_500":
                return httpx.Response(500, json={"name": "INTERNAL_SERVICE_ERROR"})
            if malformed_refund_json:
                return httpx.Response(201, content=b"{malformed json")
            if refund_payload is not None:
                return httpx.Response(201, json=refund_payload)
            return httpx.Response(
                201,
                json={
                    "id": "PAYPAL_REF_9001",
                    "status": refund_status,
                    "amount": {"value": captured_amount, "currency_code": captured_currency},
                },
            )

        return httpx.Response(404, json={"error": "not_found"})

    return httpx.MockTransport(handler)


async def _create_paypal_session(
    client: httpx.AsyncClient,
    crawler_id: str,
) -> tuple[dict[str, Any], str]:
    challenge_response = await client.get(
        "/gateway/news",
        headers={"X-Crawler-Id": crawler_id},
    )
    assert challenge_response.status_code == 402
    challenge = challenge_response.json()

    deposit_response = await client.post(
        "/deposits/create",
        json={"challenge_id": challenge["challenge_id"], "crawler_id": crawler_id},
    )
    assert deposit_response.status_code == 201
    deposit = deposit_response.json()

    capture_response = await client.post(
        f"/deposits/{deposit['deposit_id']}/capture",
        json={"crawler_id": crawler_id, "challenge_id": challenge["challenge_id"]},
    )
    assert capture_response.status_code == 200

    session_response = await client.post(
        "/sessions/accept-policy",
        json={
            "challenge_id": challenge["challenge_id"],
            "deposit_id": deposit["deposit_id"],
            "crawler_id": crawler_id,
            "policy_version": challenge["policy_version"],
            "agree_to_terms": True,
        },
    )
    assert session_response.status_code == 201
    return deposit, session_response.json()["access_token"]


@pytest.mark.asyncio
async def test_paypal_sandbox_full_compliant_lifecycle(test_settings: Settings) -> None:
    test_settings.payment_mode = "paypal_sandbox"
    counters: dict[str, int] = {}
    transport = _build_paypal_mock_transport(call_counters=counters)
    provider = PayPalSandboxProvider(test_settings, transport=transport)
    app = create_app(settings=test_settings, payment_provider=provider)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        chg = (
            await client.get("/gateway/news", headers={"X-Crawler-Id": "paypal-bot-1"})
        ).json()
        dep = (
            await client.post(
                "/deposits/create",
                json={"challenge_id": chg["challenge_id"], "crawler_id": "paypal-bot-1"},
            )
        ).json()
        assert dep["payment_mode"] == "paypal_sandbox"
        assert dep["simulated"] is False
        assert dep["provider_order_id"] == "PAYPAL_ORD_1001"
        assert "checkoutnow" in dep["approval_url"]

        cap = await client.post(
            f"/deposits/{dep['deposit_id']}/capture",
            json={"crawler_id": "paypal-bot-1", "challenge_id": chg["challenge_id"]},
        )
        assert cap.status_code == 200
        assert cap.json()["provider_capture_id"] == "PAYPAL_CAP_5001"

        ver = await client.post(
            f"/deposits/{dep['deposit_id']}/verify",
            json={"crawler_id": "paypal-bot-1", "challenge_id": chg["challenge_id"]},
        )
        assert ver.status_code == 200
        assert ver.json()["payment_status"] == "CAPTURED"

        ses = (
            await client.post(
                "/sessions/accept-policy",
                json={
                    "challenge_id": chg["challenge_id"],
                    "deposit_id": dep["deposit_id"],
                    "crawler_id": "paypal-bot-1",
                    "policy_version": chg["policy_version"],
                    "agree_to_terms": True,
                },
            )
        ).json()
        headers = {"Authorization": f"Bearer {ses['access_token']}"}

        page = await client.get("/gateway/news", headers=headers)
        assert page.status_code == 200

        comp = await client.post("/sessions/complete", headers=headers)
        assert comp.status_code == 200
        final_dep = comp.json()["deposit"]
        assert final_dep["deposit_state"] == "REFUNDED"
        assert final_dep["refund_status"] == "REFUNDED"
        assert final_dep["provider_refund_id"] == "PAYPAL_REF_9001"
        assert counters["/v2/payments/captures/PAYPAL_CAP_5001/refund"] == 1


async def _call_paypal_refund(
    test_settings: Settings,
    **transport_options: Any,
) -> tuple[Any, dict[str, int]]:
    counters: dict[str, int] = {}
    provider = PayPalSandboxProvider(
        test_settings,
        transport=_build_paypal_mock_transport(
            call_counters=counters,
            **transport_options,
        ),
    )
    result = await provider.refund_capture(
        capture_id="PAYPAL_CAP_5001",
        order_id="PAYPAL_ORD_1001",
        amount=Decimal("0.01"),
        currency="USD",
        request_id="refund-request-test",
        reason="Test refund",
    )
    return result, counters


@pytest.mark.asyncio
async def test_paypal_refund_requires_completed_status_and_matching_details(
    test_settings: Settings,
) -> None:
    result, counters = await _call_paypal_refund(test_settings)

    assert result.status == "COMPLETED"
    assert result.refund_id == "PAYPAL_REF_9001"
    assert result.refunded_amount == Decimal("0.01")
    assert result.currency == "USD"
    assert result.provider_status == "COMPLETED"
    assert counters["/v2/payments/captures/PAYPAL_CAP_5001/refund"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refund_payload",
    [
        {"status": "COMPLETED", "amount": {"value": "0.01", "currency_code": "USD"}},
        {"id": "PAYPAL_REF_9001", "status": "COMPLETED", "amount": {"value": "0.01"}},
        {"id": "PAYPAL_REF_9001", "status": "COMPLETED", "amount": {"currency_code": "USD"}},
        {
            "id": "PAYPAL_REF_9001",
            "status": "COMPLETED",
            "amount": {"value": "0.02", "currency_code": "USD"},
        },
        {
            "id": "PAYPAL_REF_9001",
            "status": "COMPLETED",
            "amount": {"value": "0.01", "currency_code": "EUR"},
        },
    ],
)
async def test_paypal_does_not_confirm_incomplete_or_mismatched_refund(
    test_settings: Settings,
    refund_payload: dict[str, Any],
) -> None:
    result, _ = await _call_paypal_refund(
        test_settings,
        refund_payload=refund_payload,
    )

    assert result.status == "PENDING"
    assert result.provider_status == "COMPLETED"


@pytest.mark.asyncio
async def test_paypal_pending_refund_is_not_reported_as_completed(
    test_settings: Settings,
) -> None:
    result, _ = await _call_paypal_refund(test_settings, refund_status="PENDING")

    assert result.status == "PENDING"
    assert result.provider_status == "PENDING"


@pytest.mark.asyncio
async def test_paypal_does_not_refund_when_reconciliation_omits_refund_data(
    test_settings: Settings,
) -> None:
    result, counters = await _call_paypal_refund(
        test_settings,
        include_refunds_in_order_lookup=False,
    )

    assert result.status == "PENDING"
    assert "did not include refund information" in (result.failure_reason or "")
    assert counters.get("/v2/payments/captures/PAYPAL_CAP_5001/refund", 0) == 0


@pytest.mark.asyncio
async def test_paypal_token_failure_does_not_submit_refund(
    test_settings: Settings,
) -> None:
    result, counters = await _call_paypal_refund(test_settings, token_status=401)

    assert result.status == "FAILED"
    assert result.refund_id is None
    assert counters["/v1/oauth2/token"] == 1
    assert "/v2/checkout/orders/PAYPAL_ORD_1001" not in counters
    assert "/v2/payments/captures/PAYPAL_CAP_5001/refund" not in counters


@pytest.mark.asyncio
async def test_paypal_refund_timeout_is_unknown_and_reconciled_before_retry(
    test_settings: Settings,
) -> None:
    counters: dict[str, int] = {}
    provider = PayPalSandboxProvider(
        test_settings,
        transport=_build_paypal_mock_transport(
            timeout_on_refund=True,
            call_counters=counters,
        ),
    )

    first = await provider.refund_capture(
        capture_id="PAYPAL_CAP_5001",
        order_id="PAYPAL_ORD_1001",
        amount=Decimal("0.01"),
        currency="USD",
        request_id="refund-request-test",
        reason="Test refund",
    )
    second = await provider.refund_capture(
        capture_id="PAYPAL_CAP_5001",
        order_id="PAYPAL_ORD_1001",
        amount=Decimal("0.01"),
        currency="USD",
        request_id="refund-request-test",
        reason="Test refund",
    )

    assert first.status == "PENDING"
    assert "outcome is unknown" in (first.failure_reason or "")
    assert second.status == "COMPLETED"
    assert second.refund_id == "PAYPAL_REF_9001"
    assert counters["/v2/payments/captures/PAYPAL_CAP_5001/refund"] == 1


@pytest.mark.asyncio
async def test_paypal_malformed_refund_json_remains_pending(
    test_settings: Settings,
) -> None:
    result, counters = await _call_paypal_refund(
        test_settings,
        malformed_refund_json=True,
    )

    assert result.status == "PENDING"
    assert result.refund_id is None
    assert "unreadable JSON" in (result.failure_reason or "")
    assert counters["/v2/payments/captures/PAYPAL_CAP_5001/refund"] == 1


@pytest.mark.asyncio
async def test_paypal_recovers_persisted_in_progress_refund_without_duplicate(
    test_settings: Settings,
) -> None:
    test_settings.payment_mode = "paypal_sandbox"
    counters: dict[str, int] = {}
    provider = PayPalSandboxProvider(
        test_settings,
        transport=_build_paypal_mock_transport(
            timeout_on_refund=True,
            call_counters=counters,
        ),
    )
    app = create_app(settings=test_settings, payment_provider=provider)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        deposit, access_token = await _create_paypal_session(client, "crawler-refund-recovery")
        headers = {"Authorization": f"Bearer {access_token}"}

        first_completion = await client.post("/sessions/complete", headers=headers)
        assert first_completion.status_code == 200
        assert first_completion.json()["deposit"]["refund_status"] == "REFUND_PENDING"

        manager = app.state.container.deposit_manager
        async with manager._lock:
            record = manager._deposits[deposit["deposit_id"]]
            record.refund_status = RefundTransactionStatus.REFUND_IN_PROGRESS
            record.deposit_state = DepositState.REFUND_PENDING
            record.provider_refund_status = "UNKNOWN"
            manager._persist_unlocked()

        restarted_app = create_app(settings=test_settings, payment_provider=provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_app),
            base_url="http://testserver",
        ) as restarted_client:
            recovered = await restarted_client.post("/sessions/complete", headers=headers)

    assert recovered.status_code == 200
    final_deposit = recovered.json()["deposit"]
    assert final_deposit["refund_status"] == "REFUNDED"
    assert final_deposit["provider_refund_id"] == "PAYPAL_REF_9001"
    assert counters["/v2/payments/captures/PAYPAL_CAP_5001/refund"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_amount,bad_currency",
    [("0.50", "USD"), ("0.01", "EUR")],
)
async def test_paypal_rejects_mismatched_amount_or_currency(
    test_settings: Settings,
    bad_amount: str,
    bad_currency: str,
) -> None:
    test_settings.payment_mode = "paypal_sandbox"
    transport = _build_paypal_mock_transport(
        captured_amount=bad_amount,
        captured_currency=bad_currency,
    )
    provider = PayPalSandboxProvider(test_settings, transport=transport)
    app = create_app(settings=test_settings, payment_provider=provider)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        chg = (
            await client.get("/gateway/news", headers={"X-Crawler-Id": "paypal-mismatch"})
        ).json()
        dep = (
            await client.post(
                "/deposits/create",
                json={"challenge_id": chg["challenge_id"], "crawler_id": "paypal-mismatch"},
            )
        ).json()

        cap = await client.post(
            f"/deposits/{dep['deposit_id']}/capture",
            json={"crawler_id": "paypal-mismatch", "challenge_id": chg["challenge_id"]},
        )
        assert cap.status_code == 402
        body = cap.json()
        assert body["error"] == "capture_verification_failed"
        assert body["deposit"]["payment_status"] == "FAILED"
        assert body["deposit"]["deposit_state"] == "FAILED"


@pytest.mark.asyncio
async def test_paypal_unapproved_order_and_timeout_handling(
    test_settings: Settings,
) -> None:
    test_settings.payment_mode = "paypal_sandbox"

    # 1. Timeout on order creation returns HTTP 504 and marks deposit FAILED
    timeout_provider = PayPalSandboxProvider(
        test_settings,
        transport=_build_paypal_mock_transport(timeout_on_order=True),
    )
    app_timeout = create_app(settings=test_settings, payment_provider=timeout_provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_timeout),
        base_url="http://testserver",
    ) as client:
        chg = (
            await client.get("/gateway/news", headers={"X-Crawler-Id": "bot-timeout"})
        ).json()
        resp = await client.post(
            "/deposits/create",
            json={"challenge_id": chg["challenge_id"], "crawler_id": "bot-timeout"},
        )
        assert resp.status_code == 504

    # 2. Unapproved order returns HTTP 409 with approval_url
    unapproved_provider = PayPalSandboxProvider(
        test_settings,
        transport=_build_paypal_mock_transport(require_approval_on_capture=True),
    )
    app_unapproved = create_app(
        settings=test_settings, payment_provider=unapproved_provider
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_unapproved),
        base_url="http://testserver",
    ) as client:
        chg2 = (
            await client.get("/gateway/news", headers={"X-Crawler-Id": "bot-unapproved"})
        ).json()
        dep2 = (
            await client.post(
                "/deposits/create",
                json={"challenge_id": chg2["challenge_id"], "crawler_id": "bot-unapproved"},
            )
        ).json()
        cap2 = await client.post(
            f"/deposits/{dep2['deposit_id']}/capture",
            json={"crawler_id": "bot-unapproved", "challenge_id": chg2["challenge_id"]},
        )
        assert cap2.status_code == 409
        assert cap2.json()["error"] == "payer_approval_required"
        assert "checkoutnow" in cap2.json()["approval_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("simulated_refund_outcome", ["HTTP_500", "PENDING"])
async def test_paypal_failed_or_pending_refund_preserves_captured_record(
    test_settings: Settings,
    simulated_refund_outcome: str,
) -> None:
    test_settings.payment_mode = "paypal_sandbox"
    provider = PayPalSandboxProvider(
        test_settings,
        transport=_build_paypal_mock_transport(refund_status=simulated_refund_outcome),
    )
    app = create_app(settings=test_settings, payment_provider=provider)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        chg = (
            await client.get("/gateway/news", headers={"X-Crawler-Id": "bot-refund-edge"})
        ).json()
        dep = (
            await client.post(
                "/deposits/create",
                json={
                    "challenge_id": chg["challenge_id"],
                    "crawler_id": "bot-refund-edge",
                },
            )
        ).json()
        await client.post(
            f"/deposits/{dep['deposit_id']}/capture",
            json={"crawler_id": "bot-refund-edge", "challenge_id": chg["challenge_id"]},
        )
        ses = (
            await client.post(
                "/sessions/accept-policy",
                json={
                    "challenge_id": chg["challenge_id"],
                    "deposit_id": dep["deposit_id"],
                    "crawler_id": "bot-refund-edge",
                    "policy_version": chg["policy_version"],
                    "agree_to_terms": True,
                },
            )
        ).json()
        headers = {"Authorization": f"Bearer {ses['access_token']}"}
        await client.get("/gateway/news", headers=headers)

        comp = await client.post("/sessions/complete", headers=headers)
        assert comp.status_code == 200
        final_dep: dict[str, Any] = comp.json()["deposit"]

        # Original captured payment details are strictly preserved!
        assert final_dep["payment_status"] == "CAPTURED"
        assert final_dep["provider_capture_id"] == "PAYPAL_CAP_5001"
        assert final_dep["captured_amount"] == "0.01"
        assert final_dep["policy_decision"] == "ELIGIBLE_FOR_REFUND"

        assert final_dep["refund_status"] == "REFUND_PENDING"
        assert final_dep["deposit_state"] == "REFUND_PENDING"
