"""Tests for deposit lifecycle, ownership isolation, refund eligibility, and idempotency."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from tests.conftest import complete_onboarding_flow


@pytest.mark.asyncio
async def test_challenge_single_use_and_ownership_isolation(
    async_client: httpx.AsyncClient,
) -> None:
    # Crawler A gets a challenge
    chg_resp = await async_client.get(
        "/gateway/news", headers={"X-Crawler-Id": "crawler-alice"}
    )
    assert chg_resp.status_code == 402
    challenge = chg_resp.json()
    cid = challenge["challenge_id"]

    # Crawler B cannot create a deposit using Crawler A's challenge
    hijack_resp = await async_client.post(
        "/deposits/create",
        json={"challenge_id": cid, "crawler_id": "crawler-mallory"},
    )
    assert hijack_resp.status_code == 403

    # Crawler A cannot supply an arbitrary deposit amount (extra field rejected with 400)
    tamper_amount = await async_client.post(
        "/deposits/create",
        json={"challenge_id": cid, "crawler_id": "crawler-alice", "amount": "0.00"},
    )
    assert tamper_amount.status_code == 400

    # Crawler A creates the deposit
    dep_resp = await async_client.post(
        "/deposits/create",
        json={"challenge_id": cid, "crawler_id": "crawler-alice"},
    )
    assert dep_resp.status_code == 201
    deposit_id = dep_resp.json()["deposit_id"]

    # Reusing the same challenge_id a second time is rejected with 409 Conflict
    reuse_chg = await async_client.post(
        "/deposits/create",
        json={"challenge_id": cid, "crawler_id": "crawler-alice"},
    )
    assert reuse_chg.status_code == 409

    # Policy acceptance BEFORE capture is rejected with 402 Payment Required
    early_accept = await async_client.post(
        "/sessions/accept-policy",
        json={
            "challenge_id": cid,
            "deposit_id": deposit_id,
            "crawler_id": "crawler-alice",
            "policy_version": challenge["policy_version"],
            "agree_to_terms": True,
        },
    )
    assert early_accept.status_code == 402

    # Crawler B cannot inspect or capture Crawler A's deposit
    inspect_mallory = await async_client.get(
        f"/deposits/{deposit_id}",
        headers={"X-Crawler-Id": "crawler-mallory", "X-Challenge-Id": cid},
    )
    assert inspect_mallory.status_code == 403

    cap_mallory = await async_client.post(
        f"/deposits/{deposit_id}/capture",
        json={"crawler_id": "crawler-mallory", "challenge_id": cid},
    )
    assert cap_mallory.status_code == 403


@pytest.mark.asyncio
async def test_compliant_vs_violating_refund_decisions_and_idempotency(
    async_client: httpx.AsyncClient,
) -> None:
    # 1. Compliant crawler flow -> ELIGIBLE_FOR_REFUND and REFUNDED
    _, dep_a, ses_a = await complete_onboarding_flow(async_client, "crawler-good")
    headers_a = {"Authorization": f"Bearer {ses_a['access_token']}"}

    r1 = await async_client.get("/gateway/news", headers=headers_a)
    assert r1.status_code == 200

    # Calling /deposits/{id}/refund BEFORE session completion returns 409 Conflict
    premature_refund = await async_client.post(
        f"/deposits/{dep_a['deposit_id']}/refund", headers=headers_a
    )
    assert premature_refund.status_code == 409

    # Concurrent session completion + duplicate refund calls are safe and idempotent
    res1, res2 = await asyncio.gather(
        async_client.post("/sessions/complete", headers=headers_a),
        async_client.post("/sessions/complete", headers=headers_a),
    )
    assert res1.status_code == 200
    assert res2.status_code == 200
    body_a = res1.json()["deposit"]
    assert body_a["deposit_state"] == "REFUNDED"
    assert body_a["payment_status"] == "CAPTURED"
    assert body_a["policy_decision"] == "ELIGIBLE_FOR_REFUND"
    assert body_a["refund_status"] == "REFUNDED"
    refund_id_first = body_a["provider_refund_id"]
    assert refund_id_first is not None

    # Calling /deposits/{id}/refund again returns the exact same refund_id idempotently
    dup_refund = await async_client.post(
        f"/deposits/{dep_a['deposit_id']}/refund", headers=headers_a
    )
    assert dup_refund.status_code == 200
    assert dup_refund.json()["provider_refund_id"] == refund_id_first

    # 2. Violating crawler flow -> FORFEITED_BY_POLICY and NOT_APPLICABLE_FORFEITED
    _, dep_b, ses_b = await complete_onboarding_flow(async_client, "crawler-bad")
    headers_b = {"Authorization": f"Bearer {ses_b['access_token']}"}

    # Violating crawler cannot refund Crawler A's deposit
    cross_refund = await async_client.post(
        f"/deposits/{dep_a['deposit_id']}/refund", headers=headers_b
    )
    assert cross_refund.status_code == 403

    # Trigger disallowed path violation
    v_resp = await async_client.get("/gateway/admin", headers=headers_b)
    assert v_resp.status_code == 403

    comp_b = await async_client.post("/sessions/complete", headers=headers_b)
    assert comp_b.status_code == 200
    data_b = comp_b.json()
    assert data_b["refund_eligible"] is False
    dep_b_final = data_b["deposit"]
    assert dep_b_final["deposit_state"] == "FORFEITED"
    assert dep_b_final["payment_status"] == "CAPTURED"
    assert dep_b_final["policy_decision"] == "FORFEITED_BY_POLICY"
    assert dep_b_final["refund_status"] == "NOT_APPLICABLE_FORFEITED"
    assert dep_b_final["provider_refund_id"] is None


@pytest.mark.asyncio
async def test_expired_challenge_is_rejected(test_settings: Settings) -> None:
    app = create_app(settings=test_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        chg_resp = await client.get(
            "/gateway/news", headers={"X-Crawler-Id": "crawler-exp"}
        )
        cid = chg_resp.json()["challenge_id"]
        # Force challenge expiration in store
        container = app.state.container
        container.deposit_manager._challenges[cid].expires_at = "2020-01-01T00:00:00+00:00"

        dep_resp = await client.post(
            "/deposits/create",
            json={"challenge_id": cid, "crawler_id": "crawler-exp"},
        )
        assert dep_resp.status_code == 400
        assert "expired" in dep_resp.json()["message"].lower()

