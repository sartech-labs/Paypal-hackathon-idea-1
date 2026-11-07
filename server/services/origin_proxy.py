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
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("sara.origin")

# Response headers worth passing through; cookies and hop-by-hop headers are dropped.
PASS_HEADERS = ("cache-control", "etag", "last-modified", "content-language", "expires", "vary")
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


@dataclass
class OriginResponse:
    status_code: int
    body: bytes
    content_type: str
    headers: dict[str, str]


class OriginError(Exception):
    """The origin could not be reached or answered with a server error."""


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

    def import_robots(self) -> RobotsRules:
        """Fetch and parse the origin's robots.txt; an unreachable file means no extra rules."""
        try:
            with httpx.Client(timeout=self._timeout, transport=self._sync_transport, follow_redirects=True) as client:
                res = client.get(urljoin(self.base_url, "robots.txt"))
            if res.status_code == 200:
                self.rules = parse_robots_txt(res.text)
            else:
                logger.warning("Origin robots.txt answered %s; enforcing SARA's own rules only", res.status_code)
        except httpx.HTTPError as exc:
            logger.warning("Could not fetch the origin's robots.txt (%s); enforcing SARA's own rules only", exc)
        return self.rules

    async def fetch(self, path: str, query: str = "") -> OriginResponse:
        url = urljoin(self.base_url, path.lstrip("/"))
        if query:
            url = f"{url}?{query}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client:
                res = await client.get(url, headers={"User-Agent": "SARA-Gateway/1.0 (+agent proxy)"})
        except httpx.HTTPError as exc:
            raise OriginError(f"Origin request failed: {exc}") from exc
        if res.status_code >= 500:
            raise OriginError(f"Origin answered {res.status_code}")
        body = res.content[:MAX_BODY_BYTES]
        headers = {k: v for k, v in res.headers.items() if k.lower() in PASS_HEADERS}
        if res.status_code in (301, 302, 303, 307, 308) and res.headers.get("location"):
            headers["location"] = res.headers["location"]
        return OriginResponse(
            status_code=res.status_code,
            body=body,
            content_type=res.headers.get("content-type", "application/octet-stream"),
            headers=headers,
        )

    def render_robots_txt(self, allowed: list[str], disallowed: list[str], notice: str) -> str:
        lines = ["# robots.txt served by the SARA gateway in front of " + self.base_url, f"# Notice: {notice}", "", "User-agent: *"]
        lines += [f"Allow: {p}" for p in allowed]
        lines += [f"Disallow: {p}" for p in disallowed]
        if self.rules.crawl_delay_seconds:
            lines.append(f"Crawl-delay: {self.rules.crawl_delay_seconds:g}")
        return "\n".join(lines) + "\n"
