"""Log in with PayPal: the owner connects the PayPal account their payouts go to."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.services.payment_service import MockPaymentProvider
from server.services.paypal_login import PayPalLogin

USERINFO = {
    "user_id": "https://www.paypal.com/webapps/auth/identity/user/abc",
    "name": "Sentinel News LLC",
    "payer_id": "PAYER123",
    "verified_account": "true",
    "emails": [{"value": "billing@sentinel.example", "primary": True, "confirmed": True}],
}


OPENID_USERINFO = {"user_id": "https://www.paypal.com/webapps/auth/identity/user/abc", "name": "Sentinel News LLC", "email": "billing@sentinel.example"}


def _fake_paypal(calls: list[str], *, openid_status: int = 200, paypal_status: int = 200, id_token: str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/oauth2/token":
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["authorization_code"] and body["code"] == ["good-code"]
            token = {"access_token": "user-token", "token_type": "Bearer"}
            if id_token:
                token["id_token"] = id_token
            return httpx.Response(200, json=token)
        assert request.headers.get("Authorization") == "Bearer user-token"
        if request.url.path == "/v1/identity/openidconnect/userinfo":
            if openid_status != 200:
                return httpx.Response(openid_status, json={"name": "INVALID_REQUEST", "message": "nope"})
            return httpx.Response(200, json=OPENID_USERINFO)
        if request.url.path == "/v1/identity/oauth2/userinfo":
            if paypal_status != 200:
                return httpx.Response(paypal_status, json={"error": "invalid_scope"})
            return httpx.Response(200, json=USERINFO)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def paypal_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(update={"payment_mode": "paypal_sandbox"})


def _client(settings: Settings, calls: list[str], **fake) -> httpx.AsyncClient:
    # Payments stay offline; only the login calls go to the fake PayPal.
    app = create_app(settings=settings, payment_provider=MockPaymentProvider())
    app.state.container.paypal_login = PayPalLogin(settings, transport=_fake_paypal(calls, **fake))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_owner_connects_paypal_and_payouts_show_the_account(paypal_settings: Settings) -> None:
    calls: list[str] = []
    admin = {"X-Admin-Token": paypal_settings.admin_api_token}
    async with _client(paypal_settings, calls) as client:
        assert (await client.post("/paypal/connect/start")).status_code == 401

        started = (await client.post("/paypal/connect/start", headers=admin)).json()
        url = urlsplit(started["authorize_url"])
        query = parse_qs(url.query)
        assert url.netloc == "www.sandbox.paypal.com" and url.path == "/connect"
        assert query["client_id"] == [paypal_settings.paypal_client_id]
        assert query["response_type"] == ["code"]
        assert "openid" in query["scope"][0] and "email" in query["scope"][0]
        assert query["redirect_uri"] == [f"{paypal_settings.app_public_url}/paypal/connect/callback"]
        state = query["state"][0]

        done = await client.get(f"/paypal/connect/callback?code=good-code&state={state}")
        # The owner goes back to the console they started from, with the outcome.
        assert done.status_code == 303
        location = done.headers["location"]
        assert location.startswith("http://testserver/console?") and location.endswith("#setup")
        assert "paypal=connected" in location and "billing%40sentinel.example" in location
        assert calls == ["POST /v1/oauth2/token", "GET /v1/identity/openidconnect/userinfo"]

        # A state works once.
        again = await client.get(f"/paypal/connect/callback?code=good-code&state={state}")
        assert again.status_code == 400

        account = (await client.get("/paypal/account", headers=admin)).json()
        assert account["login_available"] is True
        assert account["owner_connection"]["email"] == "billing@sentinel.example"
        assert account["owner_connection"]["name"] == "Sentinel News LLC"

        assert (await client.post("/paypal/connect/disconnect", headers=admin)).status_code == 200
        account = (await client.get("/paypal/account", headers=admin)).json()
        assert account["owner_connection"] is None


@pytest.mark.asyncio
async def test_callback_rejects_unknown_state_and_cancelled_logins(paypal_settings: Settings) -> None:
    calls: list[str] = []
    async with _client(paypal_settings, calls) as client:
        forged = await client.get("/paypal/connect/callback?code=good-code&state=made-up")
        assert forged.status_code == 400 and calls == []
        cancelled = await client.get("/paypal/connect/callback?error=access_denied&error_description=User+cancelled")
        assert cancelled.status_code == 400 and "User cancelled" in cancelled.text


@pytest.mark.asyncio
async def test_login_is_unavailable_with_simulated_payments(test_settings: Settings) -> None:
    app = create_app(settings=test_settings)
    admin = {"X-Admin-Token": test_settings.admin_api_token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.post("/paypal/connect/start", headers=admin)).status_code == 409
        account = (await client.get("/paypal/account", headers=admin)).json()
        assert account["login_available"] is False


async def _connect(client: httpx.AsyncClient, admin: dict) -> httpx.Response:
    started = (await client.post("/paypal/connect/start", headers=admin)).json()
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    return await client.get(f"/paypal/connect/callback?code=good-code&state={state}")


@pytest.mark.asyncio
async def test_falls_back_to_the_paypal_profile_when_openid_userinfo_fails(paypal_settings: Settings) -> None:
    calls: list[str] = []
    admin = {"X-Admin-Token": paypal_settings.admin_api_token}
    async with _client(paypal_settings, calls, openid_status=400) as client:
        done = await _connect(client, admin)
        assert done.status_code == 303 and "paypal=connected" in done.headers["location"], done.text
        account = (await client.get("/paypal/account", headers=admin)).json()
    assert account["owner_connection"]["email"] == "billing@sentinel.example"
    assert calls[-1] == "GET /v1/identity/oauth2/userinfo"


@pytest.mark.asyncio
async def test_falls_back_to_the_id_token_when_userinfo_is_refused(paypal_settings: Settings) -> None:
    import base64
    import json as _json

    claims = base64.urlsafe_b64encode(_json.dumps({"email": "owner@idtoken.example", "name": "Token Owner"}).encode()).decode().rstrip("=")
    calls: list[str] = []
    admin = {"X-Admin-Token": paypal_settings.admin_api_token}
    async with _client(paypal_settings, calls, openid_status=400, paypal_status=400, id_token=f"h.{claims}.s") as client:
        done = await _connect(client, admin)
        assert done.status_code == 303 and "paypal=connected" in done.headers["location"], done.text
        account = (await client.get("/paypal/account", headers=admin)).json()
    assert account["owner_connection"]["email"] == "owner@idtoken.example"


@pytest.mark.asyncio
async def test_a_confirmed_login_is_kept_while_paypal_withholds_the_profile(paypal_settings: Settings) -> None:
    calls: list[str] = []
    admin = {"X-Admin-Token": paypal_settings.admin_api_token}
    async with _client(paypal_settings, calls, openid_status=401, paypal_status=400) as client:
        done = await _connect(client, admin)
        assert done.status_code == 303 and "paypal=connected" in done.headers["location"], done.text
        account = (await client.get("/paypal/account", headers=admin)).json()
    owner = account["owner_connection"]
    assert owner["profile_pending"] is True and owner["email"] is None
