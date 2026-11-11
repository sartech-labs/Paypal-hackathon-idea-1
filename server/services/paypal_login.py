"""Log in with PayPal: the website owner connects the PayPal account they get paid to.

The owner signs in at PayPal (OpenID Connect). PayPal returns an authorization code to the
gateway, which exchanges it for a token and reads the account's identity (name, email, payer
ID). The connection is stored next to the gateway's other configuration.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from server.config import Settings
from server.core.security import utc_now_iso

SCOPES = "openid profile email"
STATE_TTL_SECONDS = 1800
CALLBACK_PATH = "/paypal/connect/callback"

logger = logging.getLogger("sara.paypal")


class PayPalLoginError(RuntimeError):
    """The PayPal login could not be completed."""


class PayPalLogin:
    def __init__(self, settings: Settings, transport: Optional[httpx.BaseTransport] = None) -> None:
        self._settings = settings
        self._transport = transport
        self._path = Path(settings.gateway_config_path).parent / "paypal_connection.json"
        # state -> (expiry, where to send the owner back to afterwards)
        self._states: dict[str, tuple[float, Optional[str]]] = {}
        self._lock = threading.Lock()

    # --- configuration ---------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._settings.payment_mode != "mock" and bool(
            self._settings.paypal_client_id.strip() and self._settings.paypal_client_secret.strip()
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self._settings.app_public_url.rstrip('/')}{CALLBACK_PATH}"

    def _web_host(self) -> str:
        return "https://www.sandbox.paypal.com" if self._settings.paypal_is_sandbox else "https://www.paypal.com"

    # --- the login flow --------------------------------------------------------------
    def authorize_url(self, return_to: Optional[str] = None) -> str:
        if not self.available:
            raise PayPalLoginError("PayPal is not configured on this gateway.")
        state = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._states = {s: v for s, v in self._states.items() if v[0] > now}
            self._states[state] = (now + STATE_TTL_SECONDS, return_to)
        query = urlencode(
            {
                "flowEntry": "static",
                "client_id": self._settings.paypal_client_id.strip(),
                "response_type": "code",
                "scope": SCOPES,
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return f"{self._web_host()}/connect?{query}"

    def consume_state(self, state: Optional[str]) -> tuple[bool, Optional[str]]:
        """A state is valid once, and only within its lifetime. Returns (valid, return_to)."""
        if not state:
            return False, None
        with self._lock:
            entry = self._states.pop(state, None)
        if entry is None or entry[0] <= time.time():
            return False, None
        return True, entry[1]

    def complete(self, code: str) -> dict[str, Any]:
        """Exchange the authorization code and read the account's identity."""
        base = self._settings.paypal_base_url.rstrip("/")
        auth = (self._settings.paypal_client_id.strip(), self._settings.paypal_client_secret.strip())
        try:
            with httpx.Client(base_url=base, timeout=20.0, transport=self._transport) as client:
                token = client.post(
                    "/v1/oauth2/token",
                    auth=auth,
                    data={"grant_type": "authorization_code", "code": code},
                    headers={"Accept": "application/json"},
                )
                if token.status_code != 200:
                    raise PayPalLoginError(f"PayPal did not accept the login (HTTP {token.status_code}).")
                token_body = token.json() or {}
                access = token_body.get("access_token")
                if not access:
                    raise PayPalLoginError("PayPal's response had no access token.")
                # Which permissions PayPal granted (never the token itself), for diagnosis.
                logger.info(
                    "Log in with PayPal token: scopes=%s, id_token=%s",
                    token_body.get("scope"),
                    "yes" if token_body.get("id_token") else "no",
                )
                body = self._read_userinfo(client, access)
        except httpx.HTTPError as exc:
            raise PayPalLoginError(f"Could not reach PayPal: {type(exc).__name__}.") from exc
        claims = _id_token_claims(token_body.get("id_token"))
        if not body and claims.get("email"):
            # The signed-in account's claims from the ID token PayPal returned over this
            # server-to-server TLS call (used only to name the account in the console).
            body = claims
        if body:
            connection = _connection_from_userinfo(body)
        else:
            # PayPal confirmed the login (the code was exchanged for a token) but has not
            # released the profile yet; new permissions can take hours to apply. Keep the
            # connection and name the account once PayPal shares it.
            connection = {
                "email": None,
                "name": claims.get("name"),
                "payer_id": claims.get("sub") or claims.get("user_id"),
                "verified_account": None,
                "profile_pending": True,
                "connected_at": utc_now_iso(),
            }
        self.save(connection)
        return connection

    # The standard OpenID Connect profile works with the openid/profile/email scopes. PayPal's
    # richer "paypalv1.1" profile needs extra app permissions, so it is only a fallback.
    USERINFO_ENDPOINTS = (
        ("/v1/identity/openidconnect/userinfo", {"schema": "openid"}),
        ("/v1/identity/oauth2/userinfo", {"schema": "paypalv1.1"}),
    )

    def _read_userinfo(self, client: httpx.Client, access: str) -> dict[str, Any]:
        for path, params in self.USERINFO_ENDPOINTS:
            response = client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            )
            if response.status_code == 200:
                body = response.json() or {}
                if isinstance(body, dict) and (body.get("email") or body.get("emails")):
                    return body
            else:
                logger.warning(
                    "PayPal userinfo %s answered HTTP %s: %s",
                    path,
                    response.status_code,
                    _error_summary(response),
                )
        return {}

    # --- storage ---------------------------------------------------------------------
    def load(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and (data.get("email") or data.get("connected_at")) else None

    def save(self, connection: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(connection, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self._path)

    def disconnect(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


def _connection_from_userinfo(body: dict[str, Any]) -> dict[str, Any]:
    emails = body.get("emails") or []
    primary = next((e.get("value") for e in emails if isinstance(e, dict) and e.get("primary")), None)
    email = primary or next((e.get("value") for e in emails if isinstance(e, dict) and e.get("value")), None) or body.get("email")
    if not email:
        raise PayPalLoginError("PayPal did not share an email address for this account.")
    return {
        "email": str(email),
        "name": str(body.get("name") or "").strip() or None,
        "payer_id": body.get("payer_id"),
        "verified_account": body.get("verified_account"),
        "connected_at": utc_now_iso(),
    }


def _error_summary(response: httpx.Response) -> str:
    """PayPal's error name and message, without echoing anything else from the response."""
    try:
        body = response.json() or {}
    except ValueError:
        return "(no JSON body)"
    if not isinstance(body, dict):
        return "(unexpected body)"
    name = body.get("name") or body.get("error") or ""
    message = body.get("message") or body.get("error_description") or ""
    return f"{name} {message}".strip()[:200] or "(no details)"


def _id_token_claims(id_token: Optional[str]) -> dict[str, Any]:
    """The claims inside an ID token received directly from PayPal's token endpoint."""
    if not id_token or id_token.count(".") != 2:
        return {}
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}
