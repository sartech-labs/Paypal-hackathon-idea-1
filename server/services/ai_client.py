"""AI client wrappers shared by SARA's publisher-side AI features.

Two providers implement the same TextGenerator interface:
- Claude, through the Anthropic Python SDK (Messages API with structured outputs)
- OpenAI, through the OpenAI Python SDK (Responses API with structured outputs)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

import anthropic
import openai

from server.config import Settings

logger = logging.getLogger("sara.ai")

# Server-side refusal fallback: when the primary Claude model declines, the API re-runs
# the request on Anthropic's recommended fallback model inside the same call.
SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.5"}

# OpenAI reasoning effort accepts low/medium/high; the higher settings map to "high".
OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

_ANTHROPIC_CREDENTIAL_ATTRIBUTES = ("api_key", "auth_token", "credentials", "custom_auth")


class AIUnavailableError(RuntimeError):
    """AI features are disabled, or no credentials are configured for the provider."""


class AIServiceError(RuntimeError):
    """The AI API call failed or returned a response that cannot be used."""


class AIRefusalError(AIServiceError):
    """The model declined the request."""


@dataclass(frozen=True)
class AIResult:
    text: str
    model: str
    stop_reason: str

    def json(self) -> Any:
        """Parse the response text as JSON (used with structured outputs)."""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise AIServiceError("The model returned text that is not valid JSON.") from exc


@dataclass(frozen=True)
class ToolSpec:
    """A function tool offered to the model in a tool loop."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class ToolLoopResult:
    text: str
    model: str
    steps: int
    tool_calls: int


# Runs one tool call: (name, arguments) -> (result text, is_error).
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]


class TextGenerator(Protocol):
    """Interface implemented by the provider clients and by test fakes."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def is_available(self) -> bool: ...

    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        max_tokens: int = 16000,
    ) -> AIResult: ...

    async def run_tool_loop(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        execute: ToolExecutor,
        max_steps: int = 8,
        max_tokens: int = 8000,
    ) -> ToolLoopResult: ...


# --- Claude (Anthropic) -----------------------------------------------------------------


class ClaudeTextGenerator:
    """Single-call text and JSON generation through the Anthropic Python SDK."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[anthropic.AsyncAnthropic] = None,
    ) -> None:
        self._enabled = settings.ai_enabled
        self._model = settings.ai_model or DEFAULT_MODELS["anthropic"]
        self._effort = settings.ai_effort
        self._use_refusal_fallback = settings.ai_refusal_fallback
        self._timeout = settings.ai_timeout_seconds
        self._client = client
        # With AI_PROVIDER=auto and no key at all, either provider's key would do.
        self._missing_key_message = (
            "No AI key is configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY to enable AI features."
            if settings.ai_provider == "auto"
            else "No Claude credentials are configured. Set ANTHROPIC_API_KEY to enable AI features."
        )

    @property
    def provider(self) -> str:
        return "claude"

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(timeout=self._timeout, max_retries=2)
        return self._client

    def is_available(self) -> bool:
        """True when AI is enabled and the SDK resolved some credential source."""
        if not self._enabled:
            return False
        client = self._get_client()
        # Truthiness, not "is not None": an empty ANTHROPIC_API_KEY= line in .env
        # resolves to "" and must count as missing.
        return any(bool(getattr(client, attr, None)) for attr in _ANTHROPIC_CREDENTIAL_ATTRIBUTES)

    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        max_tokens: int = 16000,
    ) -> AIResult:
        self._ensure_ready()
        output_config: dict[str, Any] = {"effort": self._effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        response = await self._create(request)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise AIServiceError("Claude returned an empty response.")
        return AIResult(text=text, model=str(response.model), stop_reason=str(response.stop_reason))

    def _ensure_ready(self) -> None:
        if not self._enabled:
            raise AIUnavailableError("AI features are disabled (AI_ENABLED=false).")
        if not self.is_available():
            raise AIUnavailableError(self._missing_key_message)

    async def _create(self, request: dict[str, Any]) -> Any:
        """Call the Messages API and map SDK errors and unusable stops to AI errors."""
        if self._use_refusal_fallback:
            request["betas"] = [SERVER_SIDE_FALLBACK_BETA]
            request["fallbacks"] = "default"
        try:
            response = await self._get_client().beta.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise AIUnavailableError("The configured Claude API key was rejected.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AIServiceError("The Claude API key lacks permission for this request.") from exc
        except anthropic.NotFoundError as exc:
            raise AIServiceError(
                f"Claude model '{self._model}' was not found; check AI_MODEL."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AIServiceError("The Claude API rate limit was reached; retry shortly.") from exc
        except anthropic.BadRequestError as exc:
            logger.warning("Claude API rejected the request: %s", exc.message)
            raise AIServiceError("The Claude API rejected the request (HTTP 400).") from exc
        except anthropic.APIStatusError as exc:
            raise AIServiceError(
                f"The Claude API returned HTTP {exc.status_code}; retry later."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AIServiceError("Could not reach the Claude API.") from exc

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise AIRefusalError(
                f"Claude declined this request (category: {category or 'unspecified'})."
            )
        if response.stop_reason == "max_tokens":
            raise AIServiceError("Claude's response was cut off at the output token limit.")
        return response

    async def run_tool_loop(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        execute: ToolExecutor,
        max_steps: int = 8,
        max_tokens: int = 8000,
    ) -> ToolLoopResult:
        """Manual tool-use loop: the model calls tools, the caller executes them."""
        self._ensure_ready()
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_defs = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                **({"strict": True} if t.strict else {}),
            }
            for t in tools
        ]
        calls = 0
        model = self._model
        for step in range(1, max_steps + 1):
            response = await self._create(
                {
                    "model": self._model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "tools": tool_defs,
                    "messages": messages,
                    "output_config": {"effort": self._effort},
                }
            )
            model = str(getattr(response, "model", model))
            # Echo the full content back so thinking and tool_use blocks stay paired.
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not uses:
                text = "".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                ).strip()
                if not text:
                    raise AIServiceError("Claude returned an empty response.")
                return ToolLoopResult(text=text, model=model, steps=step, tool_calls=calls)
            results: list[dict[str, Any]] = []
            for use in uses:
                calls += 1
                arguments = use.input if isinstance(use.input, dict) else {}
                content, is_error = await execute(use.name, arguments)
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id, "content": content}
                if is_error:
                    block["is_error"] = True
                results.append(block)
            messages.append({"role": "user", "content": results})
        raise AIServiceError(f"The assistant did not finish within {max_steps} steps.")


# --- OpenAI -----------------------------------------------------------------------------


def openai_refusal_text(response: Any) -> Optional[str]:
    """Return the refusal message from a Responses API result, if the model refused."""
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return str(getattr(part, "refusal", "") or "refused")
    return None


class OpenAITextGenerator:
    """Single-call text and JSON generation through the OpenAI Responses API."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[openai.AsyncOpenAI] = None,
    ) -> None:
        self._enabled = settings.ai_enabled
        self._model = settings.ai_model or DEFAULT_MODELS["openai"]
        self._effort = OPENAI_EFFORT[settings.ai_effort]
        self._timeout = settings.ai_timeout_seconds
        self._client = client

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            try:
                self._client = openai.AsyncOpenAI(timeout=self._timeout, max_retries=2)
            except openai.OpenAIError as exc:  # raised when no API key is configured
                raise AIUnavailableError(
                    "No OpenAI API key is configured. Set OPENAI_API_KEY to enable AI features."
                ) from exc
        return self._client

    def is_available(self) -> bool:
        if not self._enabled:
            return False
        try:
            client = self._get_client()
        except AIUnavailableError:
            return False
        return bool(getattr(client, "api_key", None))

    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Optional[dict[str, Any]] = None,
        max_tokens: int = 16000,
    ) -> AIResult:
        self._ensure_ready()
        request: dict[str, Any] = {
            "model": self._model,
            "instructions": system,
            "input": prompt,
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": self._effort},
        }
        if json_schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "sara_output",
                    "schema": json_schema,
                    "strict": True,
                }
            }

        response = await self._create(request)
        text = getattr(response, "output_text", "") or ""
        if not text.strip():
            raise AIServiceError("OpenAI returned an empty response.")
        return AIResult(text=text, model=str(response.model), stop_reason="completed")

    def _ensure_ready(self) -> None:
        if not self._enabled:
            raise AIUnavailableError("AI features are disabled (AI_ENABLED=false).")
        if not self.is_available():
            raise AIUnavailableError(
                "No OpenAI API key is configured. Set OPENAI_API_KEY to enable AI features."
            )

    async def _create(self, request: dict[str, Any]) -> Any:
        """Call the Responses API and map SDK errors and unusable results to AI errors."""
        try:
            response = await self._get_client().responses.create(**request)
        except openai.AuthenticationError as exc:
            raise AIUnavailableError("The configured OpenAI API key was rejected.") from exc
        except openai.PermissionDeniedError as exc:
            raise AIServiceError("The OpenAI API key lacks permission for this request.") from exc
        except openai.NotFoundError as exc:
            raise AIServiceError(
                f"OpenAI model '{self._model}' was not found; check AI_MODEL."
            ) from exc
        except openai.RateLimitError as exc:
            raise AIServiceError(
                "The OpenAI rate limit or quota was reached; retry shortly."
            ) from exc
        except openai.BadRequestError as exc:
            logger.warning("OpenAI API rejected the request: %s", exc.message)
            raise AIServiceError("The OpenAI API rejected the request (HTTP 400).") from exc
        except openai.APIStatusError as exc:
            raise AIServiceError(
                f"The OpenAI API returned HTTP {exc.status_code}; retry later."
            ) from exc
        except openai.APIConnectionError as exc:
            raise AIServiceError("Could not reach the OpenAI API.") from exc

        status = getattr(response, "status", None)
        if status == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            if reason == "content_filter":
                raise AIRefusalError("OpenAI declined this request (content filter).")
            raise AIServiceError("OpenAI's response was cut off at the output token limit.")
        if status not in (None, "completed"):
            raise AIServiceError(f"OpenAI returned a response with status '{status}'.")
        refusal = openai_refusal_text(response)
        if refusal is not None:
            raise AIRefusalError(f"OpenAI declined this request: {refusal[:200]}")
        return response

    async def run_tool_loop(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        execute: ToolExecutor,
        max_steps: int = 8,
        max_tokens: int = 8000,
    ) -> ToolLoopResult:
        """Manual function-calling loop over the Responses API."""
        self._ensure_ready()
        items: list[Any] = [{"role": "user", "content": prompt}]
        tool_defs = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
                "strict": t.strict,
            }
            for t in tools
        ]
        calls = 0
        model = self._model
        for step in range(1, max_steps + 1):
            response = await self._create(
                {
                    "model": self._model,
                    "instructions": system,
                    "input": items,
                    "tools": tool_defs,
                    "max_output_tokens": max_tokens,
                    "reasoning": {"effort": self._effort},
                }
            )
            model = str(getattr(response, "model", model))
            output = list(getattr(response, "output", None) or [])
            # Keep every output item (reasoning, messages, calls) for the next request.
            items.extend(output)
            function_calls = [item for item in output if getattr(item, "type", None) == "function_call"]
            if not function_calls:
                text = (getattr(response, "output_text", "") or "").strip()
                if not text:
                    raise AIServiceError("OpenAI returned an empty response.")
                return ToolLoopResult(text=text, model=model, steps=step, tool_calls=calls)
            for call in function_calls:
                calls += 1
                try:
                    arguments = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = None
                if isinstance(arguments, dict):
                    content, _is_error = await execute(call.name, arguments)
                else:
                    content = json.dumps({"error": "Tool arguments were not a JSON object."})
                items.append({"type": "function_call_output", "call_id": call.call_id, "output": content})
        raise AIServiceError(f"The assistant did not finish within {max_steps} steps.")


# --- Provider selection -------------------------------------------------------------------


def _anthropic_credentials_available() -> bool:
    client = anthropic.AsyncAnthropic()
    return any(bool(getattr(client, attr, None)) for attr in _ANTHROPIC_CREDENTIAL_ATTRIBUTES)


def _openai_key_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def resolve_ai_provider(settings: Settings) -> str:
    """Return "anthropic" or "openai" for the configured (or detected) provider."""
    if settings.ai_provider in ("anthropic", "openai"):
        return settings.ai_provider
    if _anthropic_credentials_available():
        return "anthropic"
    if _openai_key_available():
        return "openai"
    return "anthropic"


def build_text_generator(settings: Settings) -> TextGenerator:
    if resolve_ai_provider(settings) == "openai":
        return OpenAITextGenerator(settings)
    return ClaudeTextGenerator(settings)
