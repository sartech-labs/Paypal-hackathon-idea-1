"""Multi-site registry, scoped configuration, gateway routing, and the /_site edge."""

from __future__ import annotations

import json

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.origin_proxy import (
    OriginHeaders,
    OriginProxy,
    OriginResponse,
    RobotsRules,
)
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _client(settings: Settings, clock: FakeClock) -> httpx.AsyncClient:
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _patch_origins(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Keep site creation and forwarding offline and deterministic."""

    monkeypatch.setattr(OriginProxy, "import_robots", lambda self: self.rules)

    async def fake_fetch(self, path: str, query: str = "", method: str = "GET", body: bytes = b"", headers=None):
        return OriginResponse(
            status_code=200,
            body=f"{self.base_url.rstrip('/')}{path}".encode(),
            content_type="text/html; charset=utf-8",
            headers=OriginHeaders([]),
        )

    async def fake_forward(
        self,
        method: str,
        path: str,
        query: str = "",
        headers=None,
        body: bytes = b"",
    ):
        return OriginResponse(
            status_code=200,
            body=f"{self.base_url.rstrip('/')}{path}".encode(),
            content_type="text/html; charset=utf-8",
            headers=OriginHeaders([]),
        )

    monkeypatch.setattr(OriginProxy, "fetch", fake_fetch)
    monkeypatch.setattr(OriginProxy, "forward", fake_forward)


async def _create_site(
    client: httpx.AsyncClient,
    origin_url: str,
    **overrides,
) -> dict:
    payload = {"origin_url": origin_url, **overrides}
    response = await client.post("/admin/sites", headers=ADMIN, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _complete_flow(
    client: httpx.AsyncClient,
    challenge_path: str,
    *,
    crawler_id: str,
    site_id: str,
) -> dict:
    headers = {"X-Crawler-Id": crawler_id, "X-SARA-Site": site_id}
    challenge_resp = await client.get(challenge_path, headers=headers)
    assert challenge_resp.status_code == 402, challenge_resp.text
    challenge = challenge_resp.json()
    assert challenge["site_id"] == site_id

    deposit = (
        await client.post(
            "/deposits/create",
            json={"challenge_id": challenge["challenge_id"], "crawler_id": crawler_id},
        )
    ).json()
    await client.post(
        f"/deposits/{deposit['deposit_id']}/capture",
        json={"crawler_id": crawler_id, "challenge_id": challenge["challenge_id"]},
    )
    await client.post(
        f"/deposits/{deposit['deposit_id']}/verify",
        json={"crawler_id": crawler_id, "challenge_id": challenge["challenge_id"]},
    )
    issued = await client.post(
        "/sessions/accept-policy",
        json={
            "challenge_id": challenge["challenge_id"],
            "deposit_id": deposit["deposit_id"],
            "crawler_id": crawler_id,
            "policy_version": challenge["policy_version"],
            "agree_to_terms": True,
        },
    )
    assert issued.status_code == 201, issued.text
    return issued.json()


@pytest.mark.asyncio
async def test_add_sites_list_and_switch_active(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        first = await _create_site(client, "http://site-a.internal", name="Site A", set_active=True)
        second = await _create_site(client, "http://site-b.internal", name="Site B", set_active=True)

        listed = (await client.get("/admin/sites", headers=ADMIN)).json()
        ids = {site["site_id"] for site in listed["sites"]}
        assert {first["site_id"], second["site_id"]} <= ids
        assert listed["active_site_id"] == second["site_id"]
        assert [site["active"] for site in listed["sites"] if site["site_id"] == second["site_id"]] == [True]

        switched = await client.post(f"/admin/sites/{first['site_id']}/activate", headers=ADMIN)
        assert switched.status_code == 200, switched.text
        body = switched.json()
        assert body["active_site_id"] == first["site_id"]
        active = next(site for site in body["sites"] if site["site_id"] == first["site_id"])
        assert active["active"] is True and active["name"] == "Site A"


@pytest.mark.asyncio
async def test_adding_a_host_twice_is_rejected(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        await _create_site(client, "http://site-a.internal")
        before = (await client.get("/admin/sites", headers=ADMIN)).json()["sites"]

        duplicate = await client.post(
            "/admin/sites", headers=ADMIN, json={"origin_url": "http://SITE-A.internal/blog"}
        )
        assert duplicate.status_code == 422, duplicate.text
        assert "already on the gateway" in duplicate.json()["message"]
        after = (await client.get("/admin/sites", headers=ADMIN)).json()["sites"]
        assert len(after) == len(before)


@pytest.mark.asyncio
async def test_config_is_scoped_to_the_requested_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        first = await _create_site(
            client,
            "http://site-a.internal",
            name="Site A",
            set_active=False,
            deposit_amount="2.00",
            allowed_paths=["/a"],
            disallowed_paths=["/a-private"],
        )
        second = await _create_site(
            client,
            "http://site-b.internal",
            name="Site B",
            set_active=False,
            deposit_amount="3.00",
            allowed_paths=["/b"],
        )

        a_cfg = (await client.get(f"/admin/config?site_id={first['site_id']}", headers=ADMIN)).json()
        b_cfg = (
            await client.get(
                "/admin/config",
                headers={**ADMIN, "X-SARA-Site": second["site_id"]},
            )
        ).json()

        assert a_cfg["site_id"] == first["site_id"]
        assert a_cfg["deposit_amount"] == "2.00"
        assert a_cfg["allowed_paths"] == ["/a"]
        assert a_cfg["disallowed_paths"] == ["/a-private"]
        assert b_cfg["site_id"] == second["site_id"]
        assert b_cfg["deposit_amount"] == "3.00"
        assert b_cfg["allowed_paths"] == ["/b"]

        updated = await client.put(
            f"/admin/config?site_id={first['site_id']}",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"deposit_amount": "4.50"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["deposit_amount"] == "4.50"
        # The other site keeps its own deposit.
        b_again = (await client.get(f"/admin/config?site_id={second['site_id']}", headers=ADMIN)).json()
        assert b_again["deposit_amount"] == "3.00"


@pytest.mark.asyncio
async def test_policies_respect_x_sara_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        first = await _create_site(
            client,
            "http://site-a.internal",
            set_active=False,
            allowed_paths=["/a"],
            disallowed_paths=["/a-private"],
            pricing_tiers=[],
        )
        second = await _create_site(
            client,
            "http://site-b.internal",
            set_active=False,
            allowed_paths=["/b"],
            disallowed_paths=["/b-private"],
            pricing_tiers=[],
        )

        a_policy = (await client.get("/policies", headers={"X-SARA-Site": first["site_id"]})).json()
        b_policy = (await client.get("/policies", headers={"X-SARA-Site": second["site_id"]})).json()
        assert a_policy["site_id"] == first["site_id"]
        assert a_policy["allowed_paths"] == ["/a"]
        assert a_policy["disallowed_paths"] == ["/a-private"]
        assert a_policy["pricing_tiers"] == []
        assert b_policy["site_id"] == second["site_id"]
        assert b_policy["disallowed_paths"] == ["/b-private"]


@pytest.mark.asyncio
async def test_gateway_challenge_and_policy_use_the_header_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        first = await _create_site(
            client,
            "http://site-a.internal",
            set_active=False,
            deposit_amount="2.00",
            disallowed_paths=["/blocked-a"],
        )
        second = await _create_site(
            client,
            "http://site-b.internal",
            set_active=False,
            deposit_amount="3.00",
            disallowed_paths=["/blocked-b"],
        )

        issued = await _complete_flow(
            client, "/gateway/news", crawler_id="crawler-site-a", site_id=first["site_id"]
        )
        token_headers = {
            "Authorization": f"Bearer {issued['access_token']}",
            "X-SARA-Site": first["site_id"],
        }
        blocked = await client.get("/gateway/blocked-a", headers=token_headers)
        assert blocked.status_code == 403
        assert blocked.json()["reason_code"] == "DISALLOWED_PATH"

        # A session from site A cannot be replayed against site B.
        mismatch = await client.get(
            "/gateway/blocked-b",
            headers={
                "Authorization": f"Bearer {issued['access_token']}",
                "X-SARA-Site": second["site_id"],
            },
        )
        assert mismatch.status_code == 403
        assert mismatch.json()["error"] == "site_mismatch"

        # An anonymous challenge also names the header-selected site.
        b_challenge = await client.get(
            "/gateway/whatever",
            headers={"X-Crawler-Id": "crawler-site-b", "X-SARA-Site": second["site_id"]},
        )
        assert b_challenge.status_code == 402
        assert b_challenge.json()["site_id"] == second["site_id"]
        assert b_challenge.json()["deposit"]["amount"] == "3.00"


@pytest.mark.asyncio
async def test_site_prefixed_edge_gates_agents_and_forwards_humans(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    async with _client(settings, fake_clock) as client:
        site = await _create_site(client, "http://site-a.internal", set_active=True)

        agent = await client.get(
            f"/_site/{site['site_id']}/hello", headers={"User-Agent": "crawler/1.0"}
        )
        assert agent.status_code == 402
        assert agent.json()["site_id"] == site["site_id"]

        human = await client.get(
            f"/_site/{site['site_id']}/hello", headers={"User-Agent": BROWSER_UA}
        )
        assert human.status_code == 200, human.text
        assert human.text == "http://site-a.internal/hello"

        paused = await client.put(
            f"/admin/sites/{site['site_id']}", headers=ADMIN, json={"status": "paused"}
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["status"] == "paused"
        gated = await client.get(
            f"/_site/{site['site_id']}/hello", headers={"User-Agent": "crawler/1.0"}
        )
        assert gated.status_code == 503
        assert gated.json()["error"] == "site_paused"


@pytest.mark.asyncio
async def test_demo_allows_rate_limits_above_ten_per_minute(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    assert test_settings.sara_mode == "demo"
    async with _client(test_settings, fake_clock) as client:
        response = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"rate_limit_requests": 120, "rate_limit_window_seconds": 60},
        )
        assert response.status_code == 200, response.text
        assert response.json()["rate_limit_requests"] == 120
        assert response.json()["rate_limit_window_seconds"] == 60.0


@pytest.mark.asyncio
async def test_public_can_add_public_origin_but_not_private(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    public_settings = test_settings.model_copy(update={"demo_config_public": True})
    async with _client(public_settings, fake_clock) as client:
        allowed = await client.post(
            "/admin/sites",
            headers={"Content-Type": "application/json"},
            json={"origin_url": "https://93.184.216.34", "name": "Public origin"},
        )
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["origin_url"] == "https://93.184.216.34"

        private = await client.post(
            "/admin/sites",
            headers={"Content-Type": "application/json"},
            json={"origin_url": "http://127.0.0.1:9000"},
        )
        assert private.status_code == 422
        assert "private" in private.json()["message"].lower()

        link_local = await client.post(
            "/admin/sites",
            headers={"Content-Type": "application/json"},
            json={"origin_url": "http://169.254.169.254"},
        )
        assert link_local.status_code == 422
        assert "private" in link_local.json()["message"].lower()


@pytest.mark.asyncio
async def test_delete_site_and_switch_active(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        initial = (await client.get("/admin/sites", headers=ADMIN)).json()
        assert len(initial["sites"]) == 1
        only_site = initial["sites"][0]["site_id"]
        refused = await client.delete(f"/admin/sites/{only_site}", headers=ADMIN)
        assert refused.status_code == 422

        first = await _create_site(client, "http://site-a.internal", set_active=False)
        second = await _create_site(client, "http://site-b.internal", set_active=True)
        activated = await client.post(f"/admin/sites/{first['site_id']}/activate", headers=ADMIN)
        assert activated.json()["active_site_id"] == first["site_id"]

        deleted = await client.delete(f"/admin/sites/{first['site_id']}", headers=ADMIN)
        assert deleted.status_code == 204
        remaining = (await client.get("/admin/sites", headers=ADMIN)).json()
        assert first["site_id"] not in {site["site_id"] for site in remaining["sites"]}
        # Deleting the active site activates the first site that remains.
        assert remaining["active_site_id"] == remaining["sites"][0]["site_id"]
        active = await client.post(f"/admin/sites/{second['site_id']}/activate", headers=ADMIN)
        assert active.status_code == 200
        assert active.json()["active_site_id"] == second["site_id"]

        missing = await client.get(f"/admin/config?site_id={first['site_id']}", headers=ADMIN)
        assert missing.status_code == 404
        assert missing.json()["error"] == "site_not_found"


@pytest.mark.asyncio
async def test_host_routing_and_registry_persistence(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    async with _client(settings, fake_clock) as client:
        first = await _create_site(
            client,
            "http://site-a.internal",
            set_active=False,
            deposit_amount="2.00",
            allowed_paths=["/a"],
        )
        second = await _create_site(
            client,
            "http://site-b.internal",
            set_active=True,
            deposit_amount="3.00",
            allowed_paths=["/b"],
        )

        by_host_a = (await client.get("/policies", headers={"Host": "site-a.internal"})).json()
        by_host_b = (await client.get("/policies", headers={"Host": "site-b.internal"})).json()
        unknown = (await client.get("/policies", headers={"Host": "unknown.internal"})).json()
        assert by_host_a["site_id"] == first["site_id"]
        assert by_host_a["allowed_paths"] == ["/a"]
        assert by_host_b["site_id"] == second["site_id"]
        assert by_host_b["allowed_paths"] == ["/b"]
        assert unknown["site_id"] == second["site_id"]

        challenge = await client.get(
            "/gateway/news",
            headers={"Host": "site-a.internal", "X-Crawler-Id": "host-routed-agent"},
        )
        assert challenge.status_code == 402
        assert challenge.json()["site_id"] == first["site_id"]
        assert challenge.json()["deposit"]["amount"] == "2.00"

    # The registry survives a restart, including each site's own config and active id.
    async with _client(settings, fake_clock) as restarted:
        listed = (await restarted.get("/admin/sites", headers=ADMIN)).json()
        assert listed["active_site_id"] == second["site_id"]
        assert {first["site_id"], second["site_id"]} <= {
            site["site_id"] for site in listed["sites"]
        }
        a_cfg = (
            await restarted.get(f"/admin/config?site_id={first['site_id']}", headers=ADMIN)
        ).json()
        b_cfg = (
            await restarted.get(f"/admin/config?site_id={second['site_id']}", headers=ADMIN)
        ).json()
        assert a_cfg["allowed_paths"] == ["/a"] and a_cfg["deposit_amount"] == "2.00"
        assert b_cfg["allowed_paths"] == ["/b"] and b_cfg["deposit_amount"] == "3.00"
        assert a_cfg["active_site_id"] == second["site_id"]


@pytest.mark.asyncio
async def test_legacy_flat_config_is_migrated_without_writing_until_mutation(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    legacy = {
        "origin_url": "https://legacy.example",
        "deposit_amount": "7.00",
        "allowed_paths": ["/legacy"],
        "disallowed_paths": ["/legacy-private"],
        "policy_version": "2.0",
        "version": 4,
    }
    original = json.dumps(legacy, indent=2) + "\n"
    test_settings.gateway_config_path.write_text(original, encoding="utf-8")

    async with _client(test_settings, fake_clock) as client:
        current = (await client.get("/admin/config", headers=ADMIN)).json()
        assert current["site_id"] == "legacy-example"
        assert current["site_name"] == "legacy.example"
        assert current["deposit_amount"] == "7.00"
        assert current["allowed_paths"] == ["/legacy"]
        assert current["disallowed_paths"] == ["/legacy-private"]
        assert current["policy_version"] == "2.0"
        # Loading a legacy config is an in-memory migration; startup must not rewrite it.
        assert test_settings.gateway_config_path.read_text(encoding="utf-8") == original

        updated = await client.put(
            "/admin/config",
            headers={**ADMIN, "Content-Type": "application/json"},
            json={"deposit_amount": "8.00"},
        )
        assert updated.status_code == 200, updated.text

    stored = json.loads(test_settings.gateway_config_path.read_text(encoding="utf-8"))
    assert stored["active_site_id"] == "legacy-example"
    assert len(stored["sites"]) == 1
    migrated = stored["sites"][0]
    assert migrated["site_id"] == "legacy-example"
    assert migrated["name"] == "legacy.example"
    assert migrated["config"]["deposit_amount"] == "8.00"
    assert migrated["config"]["allowed_paths"] == ["/legacy"]


async def test_new_site_has_no_bundled_demo_rules_and_imports_origin_robots(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    def fake_import(self):  # type: ignore[no-untyped-def]
        self.rules = RobotsRules(
            allow=["/robots-allowed"], disallow=["/robots-disallowed"]
        )
        self.robots_imported = True
        return self.rules

    monkeypatch.setattr(OriginProxy, "import_robots", fake_import)
    async with _client(test_settings, fake_clock) as client:
        site = await _create_site(client, "http://site-a.internal", set_active=True)

        # A newly added website does not inherit the bundled demo pages or pricing tiers.
        assert "/news" not in site["allowed_paths"]
        assert "/admin" not in site["disallowed_paths"]
        assert site["pricing_tiers"] == []
        # The origin's robots.txt is imported and merged immediately.
        assert site["allowed_paths"] == ["/robots-allowed"]
        assert site["disallowed_paths"] == ["/robots-disallowed"]
        assert site["origin_status"]["robots_imported"] is True

        stored = (await client.get("/admin/config", headers=ADMIN)).json()
        assert stored["allowed_paths"] == ["/robots-allowed"]
        assert stored["disallowed_paths"] == ["/robots-disallowed"]
        assert stored["pricing_tiers"] == []


@pytest.mark.asyncio
async def test_session_limits_and_gateway_follow_the_deposit_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        first = await _create_site(
            client,
            "http://site-a.internal",
            set_active=False,
            deposit_amount="2.00",
            rate_limit_requests=7,
            rate_limit_window_seconds=30,
            max_requests_per_session=5,
        )
        second = await _create_site(
            client,
            "http://site-b.internal",
            set_active=True,
            deposit_amount="3.00",
            rate_limit_requests=9,
            rate_limit_window_seconds=60,
            max_requests_per_session=6,
        )

        issued = await _complete_flow(
            client, "/gateway/news", crawler_id="crawler-site-a", site_id=first["site_id"]
        )
        # The session uses the deposit's site limits, not the active site's (B).
        assert issued["max_requests_per_session"] == 5
        assert issued["rate_limit_requests"] == 7
        assert issued["rate_limit_window_seconds"] == 30

        token = f"Bearer {issued['access_token']}"
        allowed = await client.get(
            "/gateway/news",
            headers={"Authorization": token, "X-SARA-Site": first["site_id"]},
        )
        assert allowed.status_code == 200, allowed.text

        mismatch = await client.get(
            "/gateway/news",
            headers={"Authorization": token, "X-SARA-Site": second["site_id"]},
        )
        assert mismatch.status_code == 403
        assert mismatch.json()["error"] == "site_mismatch"


@pytest.mark.asyncio
async def test_playground_actions_use_the_selected_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        first = await _create_site(
            client,
            "http://site-a.internal",
            set_active=False,
            deposit_amount="2.00",
            rate_limit_requests=7,
            rate_limit_window_seconds=30,
            max_requests_per_session=5,
        )
        await _create_site(
            client, "http://site-b.internal", set_active=True, deposit_amount="3.00"
        )

        response = await client.get(
            f"/playground/actions?site_id={first['site_id']}", headers=ADMIN
        )
        assert response.status_code == 200, response.text
        limits = response.json()["limits"]
        assert limits["deposit_amount"] == "2.00"
        assert limits["rate_limit_requests"] == 7
        assert limits["max_requests_per_session"] == 5

        missing = await client.get("/playground/actions?site_id=missing", headers=ADMIN)
        assert missing.status_code == 404
        assert missing.json()["error"] == "site_not_found"


@pytest.mark.asyncio
async def test_public_config_can_sync_robots(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    public_settings = test_settings.model_copy(update={"demo_config_public": True})
    async with _client(public_settings, fake_clock) as client:
        response = await client.post("/admin/config/sync-robots")
        assert response.status_code == 200, response.text
        assert response.json()["site_id"] == "default"


@pytest.mark.asyncio
async def test_demo_edge_uses_the_active_site_not_the_host(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    assert test_settings.sara_mode == "demo"
    async with _client(test_settings, fake_clock) as client:
        await _create_site(client, "http://site-a.internal", set_active=False)
        await _create_site(client, "http://site-b.internal", set_active=True)

        human = await client.get(
            "/hello",
            headers={"User-Agent": BROWSER_UA, "Host": "site-a.internal"},
        )
        assert human.status_code == 200, human.text
        assert human.text == "http://site-b.internal/hello"

        agent = await client.get(
            "/hello",
            headers={"User-Agent": "crawler/1.0", "Host": "site-a.internal"},
        )
        assert agent.status_code == 402
        assert agent.json()["site_id"] == "site-b-internal"


@pytest.mark.asyncio
async def test_public_config_can_sync_robots_for_the_selected_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    public_settings = test_settings.model_copy(update={"demo_config_public": True})
    async with _client(public_settings, fake_clock) as client:
        site = await _create_site(
            client, "http://site-a.internal", set_active=False, allowed_paths=["/a"]
        )
        synced = await client.post(f"/admin/config/sync-robots?site_id={site['site_id']}")
        assert synced.status_code == 200, synced.text
        assert synced.json()["site_id"] == site["site_id"]
        assert synced.json()["allowed_paths"] == ["/a"]
        # The active site was not touched by the public, site-scoped sync.
        active = (await client.get("/admin/config")).json()
        assert active["site_id"] != site["site_id"]


@pytest.mark.asyncio
async def test_registry_is_not_written_until_the_first_mutation(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    assert not test_settings.gateway_config_path.exists()
    async with _client(test_settings, fake_clock) as client:
        assert (await client.get("/policies")).status_code == 200
        listed = await client.get("/admin/sites", headers=ADMIN)
        assert listed.status_code == 200
        assert len(listed.json()["sites"]) == 1
        # Merely reading the default in-memory site must not persist a registry.
        assert not test_settings.gateway_config_path.exists()

        created = await _create_site(client, "http://site-a.internal")
        assert created["site_id"] == "site-a-internal"
        assert test_settings.gateway_config_path.exists()


@pytest.mark.asyncio
async def test_paused_site_blocks_host_routed_gateway_traffic(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    settings = test_settings.model_copy(update={"sara_mode": "gateway"})
    async with _client(settings, fake_clock) as client:
        site = await _create_site(client, "http://site-a.internal", set_active=True)
        paused = await client.put(
            f"/admin/sites/{site['site_id']}", headers=ADMIN, json={"status": "paused"}
        )
        assert paused.status_code == 200, paused.text

        response = await client.get(
            "/gateway/news",
            headers={"Host": "site-a.internal", "X-Crawler-Id": "paused-site-agent"},
        )
        assert response.status_code == 503
        assert response.json()["error"] == "site_paused"


def test_page_plan_uses_root_for_a_new_site_without_paths() -> None:
    from server.services.playground import _page_plan

    plan = _page_plan({"allowed_paths": [], "disallowed_paths": [], "pricing_tiers": []})
    assert plan.challenge == "/"
    assert plan.pages == ("/",)

    # A disallowed root falls through to /news, even when the site advertises no other path.
    blocked_root = _page_plan(
        {"allowed_paths": [], "disallowed_paths": ["/"], "pricing_tiers": []}
    )
    assert blocked_root.challenge == "/news"

    # When every fallback is blocked there is no usable path; the playground still chooses
    # the root so the run reports the policy refusal rather than guessing another page.
    all_blocked = _page_plan(
        {"allowed_paths": [], "disallowed_paths": ["/", "/news"], "pricing_tiers": []}
    )
    assert all_blocked.challenge == "/"

    # The bundled demo policy still falls back to /news only when it has no usable paths.
    legacy = _page_plan(
        {"allowed_paths": ["/news"], "disallowed_paths": ["/news"], "pricing_tiers": []}
    )
    assert legacy.challenge == "/news"


@pytest.mark.asyncio
async def test_public_config_also_opens_the_console_playground_and_dashboard(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    public_settings = test_settings.model_copy(
        update={"demo_config_public": True, "demo_public": False, "dashboard_public": False}
    )
    async with _client(public_settings, fake_clock) as client:
        actions = await client.get("/playground/actions")
        assert actions.status_code == 200
        assert actions.json()["can_start"] is True

        dashboard = await client.get("/dashboard/data")
        assert dashboard.status_code == 200
        assert dashboard.json()["access"] == "public"


@pytest.mark.asyncio
async def test_demo_root_gates_an_explicitly_selected_site(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        site = await _create_site(client, "http://site-a.internal", set_active=True)

        # The playground (and any agent naming a site) gets a real 402 at "/".
        agent = await client.get(
            "/", headers={"User-Agent": "crawler/1.0", "X-SARA-Site": site["site_id"]}
        )
        assert agent.status_code == 402, agent.text
        assert agent.json()["site_id"] == site["site_id"]

        # A human explicitly targeting the site still reaches the origin.
        human = await client.get(
            "/", headers={"User-Agent": BROWSER_UA, "X-SARA-Site": site["site_id"]}
        )
        assert human.status_code == 200, human.text
        assert human.text == "http://site-a.internal/"

        # Without a site, demo "/" remains the console even for a non-browser client.
        console = await client.get("/", headers={"User-Agent": "crawler/1.0"})
        assert console.status_code == 200
        assert "SARA console" in console.text


@pytest.mark.asyncio
async def test_a_fresh_gateway_can_start_with_no_website(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    empty = test_settings.model_copy(update={"seed_default_site": False})
    async with _client(empty, fake_clock) as client:
        listed = (await client.get("/admin/sites", headers=ADMIN)).json()
        assert listed["sites"] == [] and listed["active_site_id"] is None
        assert (await client.get("/admin/config", headers=ADMIN)).status_code == 200
        assert (await client.get("/health")).status_code == 200
        # An agent arriving before any website exists gets an answer, not a server error.
        assert (await client.get("/news", headers={"User-Agent": "TestBot/1.0"})).status_code < 500

        first = await _create_site(client, "http://first.internal", set_active=True)
        listed = (await client.get("/admin/sites", headers=ADMIN)).json()
        assert [site["site_id"] for site in listed["sites"]] == [first["site_id"]]
        assert listed["active_site_id"] == first["site_id"]

        # Without the bundled demo site, the owner may remove their last website again.
        removed = await client.delete(f"/admin/sites/{first['site_id']}", headers=ADMIN)
        assert removed.status_code in (200, 204), removed.text
        listed = (await client.get("/admin/sites", headers=ADMIN)).json()
        assert listed["sites"] == [] and listed["active_site_id"] is None


@pytest.mark.asyncio
async def test_seeded_gateway_keeps_its_last_website(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    async with _client(test_settings, fake_clock) as client:
        listed = (await client.get("/admin/sites", headers=ADMIN)).json()
        assert len(listed["sites"]) == 1
        only = listed["sites"][0]["site_id"]
        refused = await client.delete(f"/admin/sites/{only}", headers=ADMIN)
        assert refused.status_code >= 400


@pytest.mark.asyncio
async def test_dashboard_counts_each_website_separately(
    test_settings: Settings, fake_clock: FakeClock, monkeypatch
) -> None:
    _patch_origins(monkeypatch)
    empty = test_settings.model_copy(update={"seed_default_site": False})
    async with _client(empty, fake_clock) as client:
        site_a = await _create_site(client, "http://site-a.internal", set_active=True, disallowed_paths=["/admin"])
        site_b = await _create_site(client, "http://site-b.internal", set_active=False)

        issued_a = await _complete_flow(client, "/gateway/news", crawler_id="crawler-a", site_id=site_a["site_id"])
        await _complete_flow(client, "/gateway/news", crawler_id="crawler-b", site_id=site_b["site_id"])
        # One allowed read and one forbidden path on site A only.
        token_a = {"Authorization": f"Bearer {issued_a['access_token']}", "X-SARA-Site": site_a["site_id"]}
        assert (await client.get("/gateway/news", headers=token_a)).status_code == 200
        assert (await client.get("/gateway/admin", headers=token_a)).status_code == 403

        async def data(site: str) -> dict:
            response = await client.get(f"/dashboard/data?site={site}", headers=ADMIN)
            assert response.status_code == 200, response.text
            return response.json()

        only_a, only_b, both = await data(site_a["site_id"]), await data(site_b["site_id"]), await data("all")
        assert {d["crawler_id"] for d in only_a["deposits"]} == {"crawler-a"}
        assert {d["crawler_id"] for d in only_b["deposits"]} == {"crawler-b"}
        assert {d["crawler_id"] for d in both["deposits"]} == {"crawler-a", "crawler-b"}
        assert [s["crawler_id"] for s in only_b["sessions"]] == ["crawler-b"]

        assert only_a["kpis"]["policy_violations"] >= 1
        assert only_b["kpis"]["policy_violations"] == 0
        assert only_a["kpis"]["gateway_requests_allowed"] >= 1
        assert only_b["kpis"]["gateway_requests_allowed"] == 0
        assert all(e["crawler_id"] != "crawler-a" for e in only_b["events"])
        assert both["kpis"]["policy_violations"] == only_a["kpis"]["policy_violations"]

        history_b = await client.get(f"/demo/requests?site={site_b['site_id']}", headers=ADMIN)
        assert history_b.status_code == 200, history_b.text
        assert all(row["crawler_id"] == "crawler-b" for row in history_b.json()["rows"])

        assert (await client.get("/dashboard/data?site=nope", headers=ADMIN)).status_code == 404

        # Removing a website removes its activity from "all websites".
        assert (await client.delete(f"/admin/sites/{site_a['site_id']}", headers=ADMIN)).status_code in (200, 204)
        after = await data("all")
        assert {d["crawler_id"] for d in after["deposits"]} == {"crawler-b"}
        assert after["kpis"]["policy_violations"] == 0
