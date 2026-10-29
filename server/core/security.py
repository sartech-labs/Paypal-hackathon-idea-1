"""Security primitives, path normalization, and atomic persistence helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote


class PersistenceCorruptionError(RuntimeError):
    """Raised when a persisted state file is corrupted or malformed."""


class StateTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""


CRAWLER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._:-]{2,128}$")
SENSITIVE_KEY_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "authorization",
    "client_secret",
    "admin_api_token",
)


def utc_now() -> datetime:
    """Return current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return utc_now().isoformat()


def utc_future_iso(seconds: int | float) -> str:
    """Return future UTC timestamp in ISO-8601 format."""
    return (utc_now() + timedelta(seconds=float(seconds))).isoformat()


def parse_utc_iso(timestamp_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string into a UTC timezone-aware datetime."""
    dt = datetime.fromisoformat(timestamp_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_timestamp_expired(expires_at_iso: str, now: datetime | None = None) -> bool:
    """Return True if the current UTC time is at or past expires_at_iso."""
    reference = now or utc_now()
    return reference >= parse_utc_iso(expires_at_iso)


def generate_bearer_token() -> str:
    """Generate a high-entropy URL-safe random bearer token."""
    return secrets.token_urlsafe(32)


def hash_bearer_token(raw_token: str) -> str:
    """Compute SHA-256 hex digest of a high-entropy bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings in constant time."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def validate_crawler_id(crawler_id: str | None) -> str:
    """Validate a client-supplied crawler ID or raise ValueError."""
    if not crawler_id or not isinstance(crawler_id, str):
        raise ValueError("crawler_id must be a non-empty string")
    cleaned = crawler_id.strip()
    if not CRAWLER_ID_PATTERN.match(cleaned):
        raise ValueError(
            "crawler_id must be 2-128 characters containing only letters, digits, '.', '_', ':', or '-'"
        )
    return cleaned


def normalize_and_validate_gateway_path(
    path_param: str,
    raw_request_path: str | None = None,
) -> tuple[str, bool, str | None]:
    """
    Safely normalize a gateway path and detect traversal or encoding attacks.

    Returns:
        (normalized_path, is_valid, error_reason)
    """
    candidates = [path_param or ""]
    if raw_request_path is not None:
        candidates.append(raw_request_path)

    for candidate in candidates:
        lower = candidate.lower()
        # Reject null bytes, backslashes, encoded slashes/backslashes/dots, or double encoding
        if "\x00" in candidate or "%00" in lower:
            return "/", False, "Null byte in path is prohibited"
        if "\\" in candidate or "%5c" in lower:
            return "/", False, "Backslash path separator is prohibited"
        if "%2e" in lower or "%252e" in lower:
            return "/", False, "Percent-encoded dot segments are prohibited"
        if "%2f" in lower or "%252f" in lower:
            return "/", False, "Percent-encoded slash characters are prohibited"

        # Inspect two rounds of URL decoding for traversal attempts
        once = unquote(candidate)
        twice = unquote(once)
        for decoded in (candidate, once, twice):
            if "\\" in decoded or "\x00" in decoded:
                return "/", False, "Invalid control or backslash characters in path"
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
                return "/", False, "Control characters are not allowed in path"
            segments = [seg for seg in decoded.split("/") if seg != ""]
            if any(seg in (".", "..") for seg in segments):
                return "/", False, "Dot-segment path traversal is prohibited"

    cleaned = unquote(path_param or "").strip()
    if not cleaned or cleaned == "/":
        return "/", True, None

    segments = [seg for seg in cleaned.split("/") if seg != ""]
    if any(seg in (".", "..") for seg in segments):
        return "/", False, "Dot-segment path traversal is prohibited"

    normalized = "/" + "/".join(segments)
    return normalized, True, None


def sanitize_dict_for_logging(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub sensitive keys from a dictionary before logging."""
    sanitized: dict[str, Any] = {}
    for key, value in data.items():
        key_lower = str(key).lower()
        if any(s in key_lower for s in SENSITIVE_KEY_SUBSTRINGS) and key_lower not in (
            "token_type",
            "payment_context_token_id",
        ):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_dict_for_logging(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_dict_for_logging(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON data to path using a temporary file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def safe_load_json(path: Path, expected_top_keys: tuple[str, ...]) -> dict[str, Any]:
    """
    Load a persisted JSON snapshot safely.

    - Returns an empty structure with expected_top_keys if the file does not exist.
    - Raises PersistenceCorruptionError if the file is unreadable or malformed.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        return {key: {} for key in expected_top_keys}

    try:
        raw_text = path.read_text(encoding="utf-8")
        if not raw_text.strip():
            raise PersistenceCorruptionError(f"Persistence file {path} is empty")
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PersistenceCorruptionError(
            f"Malformed JSON in persistence file {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise PersistenceCorruptionError(
            f"Unable to read persistence file {path}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise PersistenceCorruptionError(
            f"Persistence file {path} root must be a JSON object"
        )

    for key in expected_top_keys:
        if key not in parsed or not isinstance(parsed[key], dict):
            raise PersistenceCorruptionError(
                f"Persistence file {path} is missing required object key '{key}'"
            )

    return parsed

