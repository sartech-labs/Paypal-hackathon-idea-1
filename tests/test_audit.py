"""Tests for append-only audit logging, secret sanitization, auth, and restart persistence."""

from __future__ import annotations

import json

import httpx
import pytest

from server.config import Settings
from server.core.security import PersistenceCorruptionError
from server.main import create_app
from tests.conftest import complete_onboarding_flow


@pytest.mark.asyncio
async def test_audit_logging_secret_sanitization_and_authorization(
    async_client: httpx.AsyncClient,
    test_settings: Settings,
) -> None:
    # Unauthenticated audit queries must fail with 401
    assert (await async_client.get("/audit/events")).status_code == 401
    assert (await async_client.get("/audit/summary")).status_code == 401

    # Invalid admin token fails with 401
    assert (
        await async_client.get(
            "/audit/summary", headers={"X-Admin-Token": "wrong-admin-key"}
        )
    ).status_code == 401

    # Run onboarding + 1 allowed + 1 violation + completion
    _, _, ses_a = await complete_onboarding_flow(async_client, "crawler-audit-a")
    raw_token_a = ses_a["access_token"]
    headers_a = {"Authorization": f"Bearer {raw_token_a}"}

    await async_client.get("/gateway/news", headers=headers_a)
    await async_client.get("/gateway/admin", headers=headers_a)
    await async_client.post("/sessions/complete", headers=headers_a)

    # Crawler A can view its own session-scoped audit events and summary
    my_events_resp = await async_client.get("/audit/events", headers=headers_a)
    assert my_events_resp.status_code == 200
    event_types = [ev["event_type"] for ev in my_events_resp.json()]
    assert "POLICY_ACCEPTED" in event_types
    assert "SESSION_ISSUED" in event_types
    assert "GATEWAY_ACCESS_ALLOWED" in event_types
    assert "POLICY_VIOLATION" in event_types
    assert "SESSION_COMPLETED" in event_types
    assert "DEPOSIT_FORFEITED" in event_types

    # Crawler A cannot query events for another crawler_id
    forbidden_q = await async_client.get(
        "/audit/events?crawler_id=someone-else", headers=headers_a
    )
    assert forbidden_q.status_code == 403

    # Admin with valid X-Admin-Token gets global summary
    admin_summary = await async_client.get(
        "/audit/summary",
        headers={"X-Admin-Token": test_settings.admin_api_token},
    )
    assert admin_summary.status_code == 200
    assert admin_summary.json()["scope"] == "global"
    assert admin_summary.json()["policy_violations"] >= 1

    # Verify raw bearer token and client_secret never appear on disk in any storage file
    for path in (
        test_settings.audit_log_path,
        test_settings.payments_db_path,
        test_settings.sessions_db_path,
    ):
        raw_disk_text = path.read_text(encoding="utf-8")
        assert raw_token_a not in raw_disk_text
        assert test_settings.paypal_client_secret not in raw_disk_text
        assert test_settings.admin_api_token not in raw_disk_text

    # Verify each line of audit.jsonl is valid JSON
    lines = [
        line
        for line in test_settings.audit_log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 7
    for line in lines:
        parsed = json.loads(line)
        assert "event_id" in parsed and "timestamp" in parsed


@pytest.mark.asyncio
async def test_persistence_survives_server_restart_and_detects_corruption(
    test_settings: Settings,
) -> None:
    # 1. Start server instance 1 and create an active session and an expired session
    app1 = create_app(settings=test_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app1),
        base_url="http://testserver",
    ) as client1:
        _, _, ses_active = await complete_onboarding_flow(client1, "crawler-persist-1")
        _, _, ses_expiring = await complete_onboarding_flow(client1, "crawler-persist-2")

        # Force ses_expiring to have an already-past expires_at in sessions_db.json
        c1 = app1.state.container
        c1.session_manager._sessions[ses_expiring["session_id"]].expires_at = (
            "2020-01-01T00:00:00+00:00"
        )
        c1.session_manager._persist_unlocked()

    # 2. Simulate server restart by creating a brand new app2 instance with the same paths
    app2 = create_app(settings=test_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2),
        base_url="http://testserver",
    ) as client2:
        # Active session still works after restart
        ok_resp = await client2.get(
            "/gateway/news",
            headers={"Authorization": f"Bearer {ses_active['access_token']}"},
        )
        assert ok_resp.status_code == 200

        # Expired session is rejected after restart with 401 SESSION_EXPIRED
        exp_resp = await client2.get(
            "/gateway/news",
            headers={"Authorization": f"Bearer {ses_expiring['access_token']}"},
        )
        assert exp_resp.status_code == 401
        assert exp_resp.json()["reason_code"] == "SESSION_EXPIRED"

    # 3. Corrupt payments_db.json and verify startup raises PersistenceCorruptionError
    test_settings.payments_db_path.write_text("{corrupted_json", encoding="utf-8")
    with pytest.raises(PersistenceCorruptionError):
        create_app(settings=test_settings)

