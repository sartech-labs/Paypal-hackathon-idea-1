"""Reverse-proxy mode: put SARA in front of an existing website.

With ORIGIN_URL set, the gateway no longer serves the bundled demo pages. It:

1. imports the origin's own robots.txt at startup and enforces its rules (the
   `User-agent: *` group's Disallow and Allow lines, plus Crawl-delay as a minimum pace),
2. treats every path the robots rules do not disallow as allowed (robots.txt semantics),
3. forwards each request that passes the deposit, policy, and rate checks to the origin
   and returns its response, so the website itself does not change.

Disallowed paths are rejected before any request reaches the origin.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import httpx

from server.core.console_auth import COOKIE_NAME

logger = logging.getLogger("sara.origin")


def _strip_console_cookie(value: str) -> str | None:
    """Remove the console session cookie from a Cookie header value.

    The signed console cookie must never reach the proxied site: that site would
    otherwise hold a credential for this console for the token's whole lifetime.
    """
    pairs = [
        pair
        for pair in value.split(";")
        if pair.strip() and pair.split("=", 1)[0].strip() != COOKIE_NAME
    ]
    return "; ".join(pair.strip() for pair in pairs) if pairs else None


def _sets_console_cookie(name: str, value: str) -> bool:
    """True when an origin response tries to set or clear the console cookie."""
    return name.lower() == "set-cookie" and value.split("=", 1)[0].strip() == COOKIE_NAME


# Response headers worth passing through to agents; cookies and hop-by-hop headers are dropped.
PASS_HEADERS = ("cache-control", "etag", "last-modified", "content-language", "expires", "vary")
# Human traffic is proxied as a normal browser request, so cookies and redirects pass through.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
        "host",
    }
)
MAX_BODY_BYTES = 10 * 1024 * 1024


@dataclass
class RobotsRules:
    """The rules from a robots.txt that apply to every crawler (`User-agent: *`)."""

    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    crawl_delay_seconds: Optional[float] = None


def parse_robots_txt(text: str) -> RobotsRules:
    """Parse the `User-agent: *` group of a robots.txt (RFC 9309).

    Groups are runs of User-agent lines followed by rules. Only groups that name `*`
    apply to every agent; rules for named bots are not merged in.
    """
    rules = RobotsRules()
    agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
            continue
        in_rules = True
        if "*" not in agents:
            continue
        if key == "disallow" and value:
            rules.disallow.append(value)
        elif key == "allow" and value:
            rules.allow.append(value)
        elif key == "crawl-delay":
            try:
                rules.crawl_delay_seconds = float(value)
            except ValueError:
                pass
    # Robots paths may end in "*" or "$" wildcards; SARA's rules match path prefixes.
    rules.allow = [p.rstrip("*$") or "/" for p in rules.allow]
    rules.disallow = [p.rstrip("*$") or "/" for p in rules.disallow]
    return rules


class OriginHeaders(dict[str, str]):
    """A header mapping that preserves duplicate values, notably Set-Cookie."""

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        self._pairs = [(key.lower(), value) for key, value in pairs]
        super().__init__()
        for key, value in self._pairs:
            super().__setitem__(key, value)

    def items(self):  # type: ignore[override]
        return iter(self._pairs)

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._pairs)

    def get_list(self, key: str) -> list[str]:
        return [value for name, value in self._pairs if name == key.lower()]

    def with_extra(self, extra: Mapping[str, str]) -> "OriginHeaders":
        return OriginHeaders([*self._pairs, *extra.items()])


@dataclass
class OriginResponse:
    status_code: int
    body: bytes
    content_type: str
    headers: OriginHeaders


class OriginError(Exception):
    """The origin could not be reached (connect, timeout, or DNS failure)."""


class OriginProxy:
    """Fetches allowed pages from the origin website on behalf of a paid, compliant agent."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sync_transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._timeout = timeout_seconds
        self._transport = transport
        self._sync_transport = sync_transport
        self.rules = RobotsRules()
        # Last import result, shown by the admin console's "test / sync robots" controls.
        self.last_checked_at: Optional[str] = None
        self.last_status_code: Optional[int] = None
        self.last_error: Optional[str] = None
        self.robots_imported = False

    def import_robots(self) -> RobotsRules:
        """Fetch and parse the origin's robots.txt; an unreachable file means no extra rules."""
        from server.core.security import utc_now_iso

        self.last_checked_at = utc_now_iso()
        self.last_status_code = None
        self.last_error = None
        self.robots_imported = False
        try:
            # Never follow redirects here: validate_origin_url only checked the first hop,
            # so a redirect could otherwise reach a private/metadata address.
            with httpx.Client(timeout=self._timeout, transport=self._sync_transport, follow_redirects=False) as client:
                res = client.get(urljoin(self.base_url, "robots.txt"))
            self.last_status_code = res.status_code
            if res.status_code == 200:
                self.rules = parse_robots_txt(res.text)
                self.robots_imported = True
            elif 500 <= res.status_code < 600:
                self.last_error = f"robots.txt answered HTTP {res.status_code}"
                logger.warning("Origin robots.txt answered %s; enforcing SARA's own rules only", res.status_code)
            else:
                # 401/403/404/410 and redirects mean "no rules here", not "site is down".
                self.rules = RobotsRules()
        except httpx.HTTPError as exc:
            self.last_error = f"Could not fetch robots.txt: {exc}"
            logger.warning("Could not fetch the origin's robots.txt (%s); enforcing SARA's own rules only", exc)
        return self.rules

    async def fetch(
        self,
        path: str,
        query: str = "",
        method: str = "GET",
        body: bytes = b"",
        headers: Optional[dict[str, str]] = None,
    ) -> OriginResponse:
        url = urljoin(self.base_url, path.lstrip("/"))
        if query:
            url = f"{url}?{query}"
        request_headers = {"User-Agent": "SARA-Gateway/1.0 (+agent proxy)"}
        request_headers.update(
            {key: value for key, value in (headers or {}).items() if key.lower() not in HOP_BY_HOP}
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client:
                res = await client.request(
                    method,
                    url,
                    headers=request_headers,
                    content=body if body else None,
                )
        except httpx.HTTPError as exc:
            raise OriginError(f"Origin request failed: {exc}") from exc
        body = res.content[:MAX_BODY_BYTES]
        header_pairs = [(k, v) for k, v in res.headers.multi_items() if k.lower() in PASS_HEADERS]
        if res.status_code in (301, 302, 303, 307, 308) and res.headers.get("location"):
            header_pairs.append(("location", res.headers["location"]))
        return OriginResponse(
            status_code=res.status_code,
            body=body,
            content_type=res.headers.get("content-type", "application/octet-stream"),
            headers=OriginHeaders(header_pairs),
        )

    async def forward(
        self,
        method: str,
        path: str,
        query: str = "",
        headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> OriginResponse:
        """Proxy a human request like a normal reverse proxy, preserving cookies and methods."""
        url = urljoin(self.base_url, path.lstrip("/"))
        if query:
            url = f"{url}?{query}"
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in HOP_BY_HOP
        }
        for key in [k for k in safe_headers if k.lower() == "cookie"]:
            filtered = _strip_console_cookie(safe_headers[key])
            if filtered is None:
                safe_headers.pop(key)
            else:
                safe_headers[key] = filtered
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client:
                res = await client.request(
                    method,
                    url,
                    headers=safe_headers,
                    content=body if body else None,
                )
        except httpx.HTTPError as exc:
            raise OriginError(f"Origin request failed: {exc}") from exc
        response_headers = OriginHeaders(
            (key, value)
            for key, value in res.headers.multi_items()
            if key.lower() not in HOP_BY_HOP and not _sets_console_cookie(key, value)
        )
        return OriginResponse(
            status_code=res.status_code,
            body=res.content[:MAX_BODY_BYTES],
            content_type=res.headers.get("content-type", "application/octet-stream"),
            headers=response_headers,
        )

    def render_robots_txt(self, allowed: list[str], disallowed: list[str], notice: str) -> str:
        lines = ["# robots.txt served by the SARA gateway in front of " + self.base_url, f"# Notice: {notice}", "", "User-agent: *"]
        lines += [f"Allow: {p}" for p in allowed]
        lines += [f"Disallow: {p}" for p in disallowed]
        if self.rules.crawl_delay_seconds:
            lines.append(f"Crawl-delay: {self.rules.crawl_delay_seconds:g}")
        return "\n".join(lines) + "\n"
