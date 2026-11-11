"""Signed console session cookies.

The cookie value has the form ``<payload>.<signature>`` where both parts are
unpadded base64url. The payload is the JSON object ``{"u": <username>, "exp":
<unix timestamp>}`` and the signature is an HMAC-SHA256 over the ASCII payload
part, keyed with the configured console session secret.

This is a deliberately small, single-user scheme: one shared console username
and password, no server-side session store, and no revocation short of rotating
``CONSOLE_SESSION_SECRET`` (changing the password or username only affects the next
sign-in) or deleting the cookie. It exists to keep the owner console private on a
public deployment, not to model a full identity system.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from server.config import Settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "sara_console"


def _b64encode(raw: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    """Decode unpadded URL-safe base64, raising on malformed input."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_console_session(
    username: str,
    secret: str,
    ttl_seconds: int,
    *,
    now: float | None = None,
) -> str:
    """Return a signed console session cookie value for ``username``."""
    issued = time.time() if now is None else now
    payload = {"u": username, "exp": int(issued + ttl_seconds)}
    payload_part = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_part}.{_b64encode(signature)}"


def read_console_session(
    token: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Verify a console cookie and return ``{"u": str, "exp": int}``, else None.

    Malformed tokens of any kind (wrong part count, bad base64, bad JSON,
    missing keys, non-integer expiry) are rejected without raising.
    """
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_part, signature_part = parts
    if not payload_part or not signature_part:
        return None
    try:
        payload_bytes = payload_part.encode("ascii")
    except UnicodeEncodeError:
        return None
    expected = hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).digest()
    try:
        provided = _b64decode(signature_part)
    except (ValueError, TypeError, binascii.Error):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64decode(payload_part).decode("utf-8"))
    except (ValueError, TypeError, binascii.Error, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("u")
    expires = payload.get("exp")
    if not isinstance(username, str) or not username:
        return None
    if isinstance(expires, bool) or not isinstance(expires, int):
        return None
    current = time.time() if now is None else now
    if expires <= current:
        return None
    return {"u": username, "exp": expires}


def console_auth_enabled(settings: "Settings") -> bool:
    """Console sign-in is required unless the operator explicitly opts out.

    ``CONSOLE_AUTH=off`` exists for public demo deployments that intentionally leave the
    console open; everything else asks for credentials.
    """
    return settings.console_auth != "off"


@dataclass(frozen=True)
class ConsoleCredentials:
    """The effective console login, resolved from env or a generated local file."""

    username: str
    password: str
    session_secret: str
    # True when no CONSOLE_PASSWORD was configured and a password was generated/stored.
    generated: bool = False


def _read_credentials(path: Path) -> dict[str, str]:
    """Load a stored credential file, returning an empty mapping on any problem."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: value for key, value in parsed.items() if isinstance(value, str)}


def _write_credentials(path: Path, payload: dict[str, str]) -> bool:
    """Atomically write the credential file with owner-only permissions."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(str(tmp_path), str(path))
        except OSError:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        logger.warning("Could not persist console credentials to %s: %s", path, exc)
        return False
    return True


def resolve_console_credentials(settings: "Settings") -> ConsoleCredentials:
    """Resolve the console login, generating and storing one when none is configured.

    ``CONSOLE_PASSWORD`` always wins. Without it, a password is generated once and kept in
    ``CONSOLE_CREDENTIALS_PATH`` so restarts and multiple workers agree. The signing key is
    persisted the same way, so sessions survive a restart and do not bounce between workers.
    """
    path = settings.console_credentials_path
    username = settings.console_username.strip() or "admin"
    configured_password = settings.console_password.strip()
    configured_secret = settings.console_session_secret.strip()

    stored = _read_credentials(path)
    password = configured_password or stored.get("password", "")
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    secret = configured_secret or stored.get("secret", "")
    if not secret:
        secret = secrets.token_urlsafe(32)

    payload = {"username": username, "secret": secret}
    # Never write an operator-supplied password to disk; only the generated one is stored,
    # because nothing else can reveal it again.
    if not configured_password:
        payload["password"] = password
    if payload != stored:
        _write_credentials(path, payload)

    if generated:
        logger.warning(
            "Console sign-in requires credentials and CONSOLE_PASSWORD is not set. "
            "Generated username=%s password=%s (stored in %s). "
            "Set CONSOLE_PASSWORD to choose your own.",
            username,
            password,
            path,
        )
    elif not configured_password:
        logger.info(
            "Console sign-in uses the password stored in %s for username=%s",
            path,
            username,
        )

    return ConsoleCredentials(
        username=username,
        password=password,
        session_secret=secret,
        generated=generated,
    )


def auth_required_redirect(path: str) -> str:
    """Build the sign-in redirect for an unauthenticated console request.

    Sign-in lives under ``/_sara/`` with the other console assets so it never shadows a
    path on the proxied origin site (``/login`` is a common origin path in gateway mode).
    """
    if not path.startswith("/") or path.startswith("//"):
        path = "/console"
    return f"/_sara/login?next={quote(path, safe='')}"
