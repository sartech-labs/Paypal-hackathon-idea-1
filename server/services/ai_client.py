"""AI client wrappers shared by CrawlerTrust's publisher-side AI features.

Two providers implement the same TextGenerator interface:
- Claude, through the Anthropic Python SDK (Messages API with structured outputs)
- OpenAI, through the OpenAI Python SDK (Responses API with structured outputs)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import anthropic
import openai

from server.config import Settings

logger = logging.getLogger("crawlertrust.ai")

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
        if not self._enabled:
            raise AIUnavailableError("AI features are disabled (AI_ENABLED=false).")
        if not self.is_available():
            raise AIUnavailableError(
                "No Claude credentials are configured. Set ANTHROPIC_API_KEY to enable AI features."
            )

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

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise AIServiceError("Claude returned an empty response.")
        return AIResult(text=text, model=str(response.model), stop_reason=str(response.stop_reason))


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
        if not self._enabled:
            raise AIUnavailableError("AI features are disabled (AI_ENABLED=false).")
        if not self.is_available():
            raise AIUnavailableError(
                "No OpenAI API key is configured. Set OPENAI_API_KEY to enable AI features."
            )

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
                    "name": "crawlertrust_output",
                    "schema": json_schema,
                    "strict": True,
                }
            }

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

        text = getattr(response, "output_text", "") or ""
        if not text.strip():
            raise AIServiceError("OpenAI returned an empty response.")
        return AIResult(text=text, model=str(response.model), stop_reason="completed")


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
