"""Test doubles for the Claude API used by CrawlerTrust's AI features."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional, Union

from server.services.ai_client import AIResult


class FakeTextGenerator:
    """Stands in for ClaudeTextGenerator; returns scripted responses in order."""

    def __init__(
        self,
        responses: Optional[list[Union[str, dict[str, Any], Exception]]] = None,
        *,
        model: str = "claude-test-model",
        available: bool = True,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self._model = model
        self._available = available

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
        if not self.responses:
            raise AssertionError("FakeTextGenerator received more calls than scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return AIResult(text=text, model=self._model, stop_reason="end_turn")


class _FakeAsyncMessages:
    def __init__(self, response: Any = None, error: Optional[Exception] = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
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
