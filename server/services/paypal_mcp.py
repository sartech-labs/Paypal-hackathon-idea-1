"""Client for PayPal's MCP server, limited to read-only tools.

CrawlerTrust's PayPal assistant reads the merchant's PayPal records through PayPal's own
Model Context Protocol (MCP) server instead of hand-written REST calls:

- local (default): runs PayPal's `@paypal/mcp` package over stdio, started with only the
  read-only tools enabled (`--tools=orders.get,...`). Needs Node.js 18+.
- hosted: connects to PayPal's remote MCP server over SSE, authenticated with the app's
  OAuth access token. Every tool is visible there, so the allowlist below is the only
  guard; the local server is preferred.

Money never moves through this module: payment, refund, void, and capture stay in
PaymentProvider, decided by deterministic code. The access token is passed to the MCP
server process or connection only; it is never sent to an AI model.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional, Protocol

import httpx

from server.config import Settings

logger = logging.getLogger("crawlertrust.paypal_mcp")

# MCP tool name -> the `@paypal/mcp --tools` selector that enables it. Read-only only.
READ_ONLY_TOOLS: dict[str, str] = {
    "get_order": "orders.get",
    "get_refund": "payments.getRefunds",
    "list_transactions": "transactions.list",
    "list_disputes": "disputes.list",
    "get_dispute": "disputes.get",
}

# Environment passed to the local MCP server process: what Node needs to run and reach
# PayPal (proxy and CA settings), nothing else. AI keys and app secrets stay out.
_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "TMPDIR",
    "SYSTEMROOT",
    "APPDATA",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "NPM_CONFIG_CACHE",
    "npm_config_cache",
)

MAX_RESULT_CHARS = 6000
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class PayPalMCPError(RuntimeError):
    """The PayPal MCP server could not be reached or returned an unusable answer."""


class PayPalMCPUnavailableError(PayPalMCPError):
    """The PayPal MCP integration is disabled or not configured."""


def redact(text: str) -> str:
    """Remove email addresses from PayPal data before it is shown or sent to a model."""
    return _EMAIL_RE.sub("[email]", text)


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPCallRecord:
    """One MCP tool call, as shown in the assistant's trace."""

    tool: str
    arguments: dict[str, Any]
    ok: bool
    duration_ms: int
    data: Any = None
    text: str = ""
    error: Optional[str] = None


class MCPToolSession(Protocol):
    tools: list[MCPTool]

    async def call(self, name: str, arguments: dict[str, Any]) -> MCPCallRecord: ...


class _RawSession(Protocol):
    """The subset of mcp.ClientSession used here (also implemented by test fakes)."""

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


# Opens an initialized MCP client session for an access token.
Connector = Callable[[str], Any]


class PayPalAccessTokenSource:
    """OAuth 2.0 client-credentials token for the MCP server, cached until near expiry."""

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._base_url = settings.paypal_base_url
        self._client_id = settings.paypal_client_id.strip()
        self._client_secret = settings.paypal_client_secret.strip()
        self._timeout = settings.paypal_timeout_seconds
        self._transport = transport
        self._token: Optional[str] = None
        self._expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def get_token(self) -> str:
        if not self.configured:
            raise PayPalMCPUnavailableError(
                "PayPal credentials (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET) are not configured."
            )
        now = time.monotonic()
        if self._token and now < self._expires_at:
            return self._token
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    "/v1/oauth2/token",
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise PayPalMCPError(f"Could not reach PayPal for an access token ({type(exc).__name__}).") from exc
        if response.status_code != 200:
            raise PayPalMCPError(f"PayPal rejected the OAuth token request (HTTP {response.status_code}).")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PayPalMCPError("PayPal returned an unreadable OAuth response.") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise PayPalMCPError("PayPal's OAuth response did not include an access token.")
        self._token = token
        self._expires_at = now + max(60, int(payload.get("expires_in", 3600)) - 120)
        return token


def local_server_command(settings: Settings, token: str) -> tuple[str, list[str], dict[str, str]]:
    """Command, arguments, and environment for the local `@paypal/mcp` server."""
    argv = shlex.split(settings.paypal_mcp_command)
    if not argv:
        raise PayPalMCPUnavailableError("PAYPAL_MCP_COMMAND is empty.")
    selectors = ",".join(sorted(set(READ_ONLY_TOOLS.values())))
    args = [arg for arg in argv[1:] if not arg.startswith("--tools")] + [f"--tools={selectors}"]
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if os.environ.get(name)}
    env["PAYPAL_ACCESS_TOKEN"] = token
    env["PAYPAL_ENVIRONMENT"] = "SANDBOX" if settings.paypal_is_sandbox else "PRODUCTION"
    return argv[0], args, env


def _unwrap(exc: BaseException) -> BaseException:
    """Return the first leaf exception of (nested) exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _default_connector(settings: Settings) -> Connector:
    @asynccontextmanager
    async def connect(token: str) -> AsyncIterator[_RawSession]:
        from mcp.client.session import ClientSession

        if settings.paypal_mcp_transport == "hosted":
            from mcp.client.sse import sse_client

            transport = sse_client(
                settings.paypal_mcp_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=settings.paypal_mcp_timeout_seconds,
                sse_read_timeout=settings.paypal_mcp_timeout_seconds * 2,
            )
        else:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            command, args, env = local_server_command(settings, token)
            transport = stdio_client(StdioServerParameters(command=command, args=args, env=env))
        async with transport as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    return connect


def _content_text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def _error_from_data(data: Any) -> Optional[str]:
    """PayPal's MCP tools report API failures inside a normal result."""
    if not isinstance(data, dict):
        return None
    if data.get("ok") is False:
        message = str(data.get("message") or data.get("code") or "PayPal returned an error.")
        status = data.get("status")
        return f"HTTP {status}: {message}" if status else message
    error = data.get("error")
    if error and "id" not in data:
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "PayPal returned an error.")
        return str(error)
    return None


class _Session:
    def __init__(self, raw: _RawSession, tools: list[MCPTool], timeout: float) -> None:
        self._raw = raw
        self.tools = tools
        self._timeout = timeout

    async def call(self, name: str, arguments: dict[str, Any]) -> MCPCallRecord:
        import anyio

        if name not in READ_ONLY_TOOLS or name not in {tool.name for tool in self.tools}:
            return MCPCallRecord(
                tool=name,
                arguments=arguments,
                ok=False,
                duration_ms=0,
                error=f"Tool '{name}' is not available to the read-only PayPal assistant.",
            )
        started = time.monotonic()
        try:
            with anyio.fail_after(self._timeout):
                result = await self._raw.call_tool(name, arguments)
        except TimeoutError:
            return MCPCallRecord(name, arguments, False, _ms(started), error="The PayPal MCP call timed out.")
        except Exception as exc:  # MCP protocol or transport failure
            leaf = _unwrap(exc)
            logger.warning("PayPal MCP call %s failed: %s", name, type(leaf).__name__)
            return MCPCallRecord(name, arguments, False, _ms(started), error=f"The PayPal MCP call failed ({type(leaf).__name__}).")

        text = redact(_content_text(result))
        try:
            data: Any = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        error = _error_from_data(data)
        if getattr(result, "is_error", False) and error is None:
            error = text[:300] or "The PayPal MCP tool reported an error."
        return MCPCallRecord(
            tool=name,
            arguments=arguments,
            ok=error is None,
            duration_ms=_ms(started),
            data=data,
            text=text[:MAX_RESULT_CHARS],
            error=error,
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


@dataclass
class PayPalMCPClient:
    """Opens read-only sessions on PayPal's MCP server."""

    settings: Settings
    token_source: Optional[PayPalAccessTokenSource] = None
    connector: Optional[Connector] = None
    last_tools: list[str] = field(default_factory=list)
    last_error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.token_source is None:
            self.token_source = PayPalAccessTokenSource(self.settings)
        if self.connector is None:
            self.connector = _default_connector(self.settings)

    @property
    def transport(self) -> str:
        return self.settings.paypal_mcp_transport

    @property
    def server_label(self) -> str:
        if self.transport == "hosted":
            return f"PayPal hosted MCP server ({self.settings.paypal_mcp_url})"
        return "PayPal MCP server (@paypal/mcp, local stdio)"

    def is_configured(self) -> bool:
        assert self.token_source is not None
        return self.settings.paypal_mcp_enabled and self.token_source.configured

    def unavailable_reason(self) -> Optional[str]:
        if not self.settings.paypal_mcp_enabled:
            return "The PayPal MCP integration is disabled (PAYPAL_MCP_ENABLED=false)."
        assert self.token_source is not None
        if not self.token_source.configured:
            return "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to connect to PayPal's MCP server."
        return None

    @asynccontextmanager
    async def open(self) -> AsyncIterator[MCPToolSession]:
        """Yield a session exposing only the read-only tools the server offers."""
        import anyio

        reason = self.unavailable_reason()
        if reason:
            raise PayPalMCPUnavailableError(reason)
        assert self.token_source is not None and self.connector is not None
        token = await self.token_source.get_token()
        timeout = self.settings.paypal_mcp_timeout_seconds
        body_error: Optional[BaseException] = None
        try:
            async with self.connector(token) as raw:
                with anyio.fail_after(timeout):
                    listed = await raw.list_tools()
                tools = [
                    MCPTool(
                        name=tool.name,
                        description=str(getattr(tool, "description", "") or ""),
                        input_schema=dict(getattr(tool, "input_schema", None) or {}),
                    )
                    for tool in getattr(listed, "tools", None) or []
                    if tool.name in READ_ONLY_TOOLS
                ]
                self.last_tools = [tool.name for tool in tools]
                self.last_error = None
                try:
                    yield _Session(raw, tools, timeout)
                except BaseException as exc:
                    body_error = exc
                    raise
        except BaseException as exc:
            # Errors from the caller's own code pass through unchanged (the MCP SDK's
            # task groups may have wrapped them in an exception group on the way out).
            if body_error is not None:
                raise body_error
            raise self._setup_error(exc) from exc

    def _setup_error(self, exc: BaseException) -> BaseException:
        leaf = _unwrap(exc)
        if isinstance(leaf, PayPalMCPError):
            return leaf
        if not isinstance(leaf, Exception):  # cancellation and interpreter exit
            return leaf
        if isinstance(leaf, FileNotFoundError):
            self.last_error = "Node.js was not found; install Node 18+ or set PAYPAL_MCP_TRANSPORT=hosted."
            return PayPalMCPUnavailableError(self.last_error)
        if isinstance(leaf, TimeoutError):
            self.last_error = "Timed out talking to the PayPal MCP server."
            return PayPalMCPError(self.last_error)
        logger.warning("PayPal MCP session failed: %s: %s", type(leaf).__name__, leaf)
        self.last_error = f"Could not use the PayPal MCP server ({type(leaf).__name__})."
        return PayPalMCPError(self.last_error)
