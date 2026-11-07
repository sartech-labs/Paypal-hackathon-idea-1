"""Verified agent identity: Web Bot Auth signatures and record-based deposits."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from client.web_bot_auth import WebBotAuth
from server.config import Settings
from server.core.agent_identity import AgentDirectory, public_jwk, sign_request, verify_request
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock

NOW = 1_800_000_000


def directory_with(key: Ed25519PrivateKey, name: str = "Good Bot") -> AgentDirectory:
    d = AgentDirectory()
    d.add(name, "Example Corp", public_jwk(key.public_key()))
    return d


def test_signature_round_trip_and_tampering() -> None:
    key = Ed25519PrivateKey.generate()
    d = directory_with(key)
    h = sign_request(key, "GET", "site.example", "/gateway/news", now=NOW)
    ok = verify_request(h, "GET", "site.example", "/gateway/news", d, now=NOW + 5)
    assert ok.verified and ok.name == "Good Bot" and ok.crawler_id == "verified:good-bot"

    assert not verify_request(h, "GET", "site.example", "/gateway/admin", d, now=NOW).verified       # other path
    assert not verify_request(h, "GET", "evil.example", "/gateway/news", d, now=NOW).verified        # other host
    assert not verify_request(h, "GET", "site.example", "/gateway/news", d, now=NOW + 3600).verified  # expired
    other = Ed25519PrivateKey.generate()
    unknown = verify_request(sign_request(other, "GET", "site.example", "/gateway/news", now=NOW), "GET", "site.example", "/gateway/news", d, now=NOW)
    assert unknown.signed and not unknown.verified and unknown.error == "Unknown agent key"
    wrong_tag = {k: v.replace("web-bot-auth", "other") for k, v in h.items()}
    assert verify_request(wrong_tag, "GET", "site.example", "/gateway/news", d, now=NOW).error.startswith("Signature tag")
    assert not verify_request({}, "GET", "site.example", "/x", d).signed


@pytest.fixture
def app_and_key(test_settings: Settings, fake_clock: FakeClock):
    app = create_app(settings=test_settings, rate_limiter=SlidingWindowRateLimiter(clock=fake_clock))
    key = Ed25519PrivateKey.generate()
    app.state.container.agent_directory.add("Good Bot", "Example Corp", public_jwk(key.public_key()))
    return app, key


async def _session(client: httpx.AsyncClient, auth: Any = None, crawler_id: str = "crawler-x") -> tuple[dict, dict]:
    chg = await client.get("/gateway/news", headers={"X-Crawler-Id": crawler_id}, auth=auth)
    assert chg.status_code == 402, chg.text
    challenge = chg.json()
    cid = challenge["crawler_id"]
    dep = (await client.post("/deposits/create", json={"challenge_id": challenge["challenge_id"], "crawler_id": cid})).json()
    ids = {"crawler_id": cid, "challenge_id": challenge["challenge_id"]}
    assert (await client.post(f"/deposits/{dep['deposit_id']}/capture", json=ids)).status_code == 200
    assert (await client.post(f"/deposits/{dep['deposit_id']}/verify", json=ids)).status_code == 200
    ses = (await client.post("/sessions/accept-policy", json={
        **ids, "deposit_id": dep["deposit_id"], "policy_version": challenge["policy_version"], "agree_to_terms": True,
    })).json()
    return challenge, ses


async def test_record_moves_the_deposit_and_repeat_offenders_are_refused(app_and_key, test_settings: Settings) -> None:
    app, key = app_and_key
    auth = WebBotAuth(key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        unsigned = (await client.get("/gateway/news", headers={"X-Crawler-Id": "anon-1"})).json()
        full = unsigned["deposit"]["amount"]
        assert unsigned["identity"]["standing"] == "unverified"

        # First visit of a known agent: verified, standard price, stable identity.
        challenge, ses = await _session(client, auth)
        assert challenge["crawler_id"] == "verified:good-bot"
        assert challenge["identity"]["standing"] == "verified" and challenge["deposit"]["amount"] == full
        bearer = {"Authorization": f"Bearer {ses['access_token']}"}
        assert (await client.get("/gateway/news", headers=bearer)).status_code == 200
        assert (await client.post("/sessions/complete", headers=bearer)).status_code == 200

        # A clean record earns the discount.
        trusted = (await client.get("/gateway/news", auth=auth)).json()
        assert trusted["identity"]["standing"] == "trusted"
        assert float(trusted["deposit"]["amount"]) < float(full)

        # Breaking the rules keeps the deposit, and the identity then pays more.
        for _ in range(test_settings.refuse_after_forfeits):
            _, ses = await _session(client, auth)
            bearer = {"Authorization": f"Bearer {ses['access_token']}"}
            assert (await client.get("/gateway/admin", headers=bearer)).status_code == 403
            await client.post("/sessions/complete", headers=bearer)
            if _ == 0:
                flagged = (await client.get("/gateway/news", auth=auth)).json()
                assert flagged["identity"]["standing"] == "flagged"
                assert float(flagged["deposit"]["amount"]) > float(full)

        refused = await client.get("/gateway/news", auth=auth)
        assert refused.status_code == 403 and refused.json()["reason_code"] == "AGENT_REFUSED"


async def test_bad_signatures_and_required_signatures(app_and_key) -> None:
    app, _ = app_and_key
    stranger = WebBotAuth(Ed25519PrivateKey.generate())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        res = await client.get("/gateway/news", auth=stranger)
        assert res.status_code == 401 and res.json()["reason_code"] == "INVALID_SIGNATURE"
        app.state.container.settings.require_signed_agents = True
        res = await client.get("/gateway/news", headers={"X-Crawler-Id": "anon-2"})
        assert res.status_code == 401 and res.json()["reason_code"] == "SIGNATURE_REQUIRED"
