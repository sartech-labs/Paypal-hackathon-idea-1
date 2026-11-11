"""Owner sign-in for the SARA console.

A signed-in owner holds an HMAC-signed session cookie. The cookie never carries the admin
token; instead :class:`OwnerSessionMiddleware` lets a request with a valid owner cookie act
with the admin token's rights, so every existing admin check keeps working unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from server.config import Settings

COOKIE_NAME = "sara_owner"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOGIN_ATTEMPTS_PER_WINDOW = 10
LOGIN_WINDOW_SECONDS = 15 * 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class OwnerAuth:
    """Issues and verifies owner session cookies, and checks owner credentials."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        secret = settings.session_secret.strip()
        self._secret = (secret or secrets.token_urlsafe(32)).encode("utf-8")
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.owner_login_enabled

    @property
    def password_login(self) -> bool:
        return bool(self.settings.owner_email.strip() and self.settings.owner_password)

    @property
    def demo_email(self) -> str:
        return self.settings.owner_email.strip() or "demo@sara.local"

    # --- credentials -------------------------------------------------------------------
    def check_password(self, email: str, password: str) -> bool:
        if not self.password_login:
            return False
        email_ok = hmac.compare_digest(
            email.strip().lower().encode("utf-8"),
            self.settings.owner_email.strip().lower().encode("utf-8"),
        )
        password_ok = hmac.compare_digest(
            password.encode("utf-8"), self.settings.owner_password.encode("utf-8")
        )
        return email_ok and password_ok

    def allow_attempt(self, client: str, now: Optional[float] = None) -> bool:
        """A small per-client limit on failed sign-ins (in memory, one process)."""
        now = time.time() if now is None else now
        with self._lock:
            recent = [t for t in self._attempts.get(client, []) if now - t < LOGIN_WINDOW_SECONDS]
            self._attempts[client] = recent
            return len(recent) < LOGIN_ATTEMPTS_PER_WINDOW

    def record_failure(self, client: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._attempts.setdefault(client, []).append(now)

    # --- cookies -----------------------------------------------------------------------
    def issue(self, email: str, *, demo: bool = False, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        payload = {
            "email": email,
            "demo": demo,
            "exp": int(now + self.settings.owner_session_hours * 3600),
        }
        body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def verify(self, cookie: Optional[str], now: Optional[float] = None) -> Optional[dict[str, Any]]:
        if not cookie or "." not in cookie:
            return None
        body, _, signature = cookie.rpartition(".")
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError):
            return None
        now = time.time() if now is None else now
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= now:
            return None
        return payload

    def max_age(self) -> int:
        return int(self.settings.owner_session_hours * 3600)

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())


def _same_origin(headers: dict[bytes, bytes]) -> bool:
    """A cookie-authenticated write must come from this site (or name no origin)."""
    origin = headers.get(b"origin")
    if not origin:
        return True
    host = headers.get(b"host", b"").decode("latin-1").lower()
    return urlsplit(origin.decode("latin-1")).netloc.lower() == host


def read_cookie(raw_cookie_header: Optional[str], name: str = COOKIE_NAME) -> Optional[str]:
    if not raw_cookie_header:
        return None
    for part in raw_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


class OwnerSessionMiddleware:
    """Give a signed-in owner the admin token's rights for this request.

    Only applies when the request has no X-Admin-Token of its own. Writes are elevated only
    for same-origin requests, so another site cannot ride the owner's cookie.
    """

    def __init__(self, app: Any, auth: OwnerAuth, admin_token: str) -> None:
        self.app = app
        self.auth = auth
        self.admin_token = admin_token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self.auth.enabled and self.admin_token:
            headers = dict(scope.get("headers") or [])
            if b"x-admin-token" not in headers:
                cookie = read_cookie(headers.get(b"cookie", b"").decode("latin-1"))
                session = self.auth.verify(cookie)
                method = str(scope.get("method", "GET")).upper()
                if session and (method in SAFE_METHODS or _same_origin(headers)):
                    scope = dict(scope)
                    scope["headers"] = list(scope.get("headers") or []) + [
                        (b"x-admin-token", self.admin_token.encode("latin-1"))
                    ]
                    scope.setdefault("state", {})
                    scope["state"] = {**scope["state"], "owner_email": session.get("email", "")}
        await self.app(scope, receive, send)
