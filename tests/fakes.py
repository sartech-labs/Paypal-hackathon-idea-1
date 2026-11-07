"""Test doubles for the AI APIs and the PayPal MCP server used by AgentTrust."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional, Union

from server.services.ai_client import AIResult, AIUnavailableError, ToolLoopResult, ToolSpec


class FakeTextGenerator:
    """Stands in for ClaudeTextGenerator; returns scripted responses in order."""

    def __init__(
        self,
        responses: Optional[list[Union[str, dict[str, Any], Exception]]] = None,
        *,
        model: str = "claude-test-model",
        available: bool = True,
        provider: str = "claude",
        tool_script: Optional[list[Any]] = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        # Tool loop script: each step is a list of (tool name, arguments) calls, a final
        # answer string, or an exception to raise.
        self.tool_script = list(tool_script or [])
        self.tool_loops: list[dict[str, Any]] = []
        self._model = model
        self._available = available
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def is_available(self) -> bool:
        return self._available

    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        max_tokens: int = 16000,
    ) -> AIResult:
        self.calls.append(
            {"system": system, "prompt": prompt, "json_schema": json_schema, "max_tokens": max_tokens}
        )
        if not self._available:
            # Like the real generators: no key configured.
            raise AIUnavailableError("No AI key is configured (fake).")
        if not self.responses:
            raise AssertionError("FakeTextGenerator received more calls than scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return AIResult(text=text, model=self._model, stop_reason="end_turn")

    async def run_tool_loop(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        execute: Any,
        max_steps: int = 8,
        max_tokens: int = 8000,
    ) -> ToolLoopResult:
        record: dict[str, Any] = {"system": system, "prompt": prompt, "tools": [t.name for t in tools], "results": []}
        self.tool_loops.append(record)
        calls = 0
        for step, item in enumerate(self.tool_script, start=1):
            if isinstance(item, Exception):
                raise item
            if isinstance(item, str):
                return ToolLoopResult(text=item, model=self._model, steps=step, tool_calls=calls)
            for name, arguments in item:
                calls += 1
                content, is_error = await execute(name, arguments)
                record["results"].append({"name": name, "content": content, "is_error": is_error})
        raise AssertionError("FakeTextGenerator tool script ended without a final answer")


class _FakeAsyncMessages:
    def __init__(self, response: Any = None, error: Optional[Exception] = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        # Snapshot the conversation: tool loops keep appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if self.error is not None:
            raise self.error
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class FakeAsyncAnthropic:
    """Minimal stand-in for anthropic.AsyncAnthropic exposing beta.messages.create."""

    def __init__(
        self,
        response: Any = None,
        error: Optional[Exception] = None,
        api_key: Optional[str] = "sk-ant-test",
    ) -> None:
        self.api_key = api_key
        self.auth_token = None
        self.credentials = None
        self.custom_auth = None
        self.beta = SimpleNamespace(messages=_FakeAsyncMessages(response, error))


class _FakeAsyncResponses:
    def __init__(self, response: Any = None, error: Optional[Exception] = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        snapshot = dict(kwargs)
        if isinstance(kwargs.get("input"), list):
            snapshot["input"] = list(kwargs["input"])
        self.calls.append(snapshot)
        if self.error is not None:
            raise self.error
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class FakeAsyncOpenAI:
    """Minimal stand-in for openai.AsyncOpenAI exposing responses.create."""

    def __init__(
        self,
        response: Any = None,
        error: Optional[Exception] = None,
        api_key: Optional[str] = "sk-test-openai",
    ) -> None:
        self.api_key = api_key
        self.responses = _FakeAsyncResponses(response, error)


def fake_openai_response(
    text: str = "{}",
    *,
    status: str = "completed",
    model: str = "gpt-5.5-2026-04-23",
    refusal: Optional[str] = None,
    incomplete_reason: Optional[str] = None,
) -> SimpleNamespace:
    """Build an object shaped like an OpenAI Responses API result."""
    content = (
        [SimpleNamespace(type="refusal", refusal=refusal)]
        if refusal
        else [SimpleNamespace(type="output_text", text=text)]
    )
    return SimpleNamespace(
        status=status,
        model=model,
        output=[SimpleNamespace(type="reasoning", summary=[]), SimpleNamespace(type="message", content=content)],
        output_text="" if refusal else text,
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
    )


def fake_message(
    text: str = "{}",
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    stop_details: Any = None,
) -> SimpleNamespace:
    """Build an object shaped like an SDK Message response."""
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
    )


# --- PayPal MCP server ------------------------------------------------------------------


class FakeMCPRawSession:
    """Stands in for mcp.ClientSession: lists tools and returns canned tool results."""

    def __init__(self, results: dict[str, Any], tools: Optional[list[str]] = None) -> None:
        self.results = results
        self.tool_names = tools or [
            "get_order",
            "get_refund",
            "list_transactions",
            "list_disputes",
            "get_dispute",
            "create_refund",
            "accept_dispute_claim",
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name, description=f"{name} tool", input_schema={"type": "object"}) for name in self.tool_names]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        value = self.results.get(name)
        if callable(value):
            value = value(arguments)
        if isinstance(value, Exception):
            raise value
        text = value if isinstance(value, str) else json.dumps(value)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)


def fake_mcp_connector(session: FakeMCPRawSession, seen_tokens: Optional[list[str]] = None) -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connect(token: str) -> Any:
        if seen_tokens is not None:
            seen_tokens.append(token)
        yield session

    return connect


class FakeTokenSource:
    configured = True

    def __init__(self, token: str = "A21-test-access-token") -> None:
        self.token = token

    async def get_token(self) -> str:
        return self.token
