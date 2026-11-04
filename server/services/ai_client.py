"""Claude API client wrapper shared by CrawlerTrust's publisher-side AI features."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import anthropic

from server.config import Settings

logger = logging.getLogger("crawlertrust.ai")

# Server-side refusal fallback: when the primary model declines, the API re-runs the
# request on Anthropic's recommended fallback model inside the same call.
SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_CREDENTIAL_ATTRIBUTES = ("api_key", "auth_token", "credentials", "custom_auth")


class AIUnavailableError(RuntimeError):
    """AI features are disabled, or no Claude credentials are configured."""


class AIServiceError(RuntimeError):
    """The Claude API call failed or returned a response that cannot be used."""


class AIRefusalError(AIServiceError):
    """Claude declined the request (stop_reason == "refusal")."""


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
            raise AIServiceError("Claude returned text that is not valid JSON.") from exc


class TextGenerator(Protocol):
    """Interface implemented by the Claude client and by test fakes."""

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


class ClaudeTextGenerator:
    """Single-call text and JSON generation through the Anthropic Python SDK."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[anthropic.AsyncAnthropic] = None,
    ) -> None:
        self._enabled = settings.ai_enabled
        self._model = settings.ai_model
        self._effort = settings.ai_effort
        self._use_refusal_fallback = settings.ai_refusal_fallback
        self._timeout = settings.ai_timeout_seconds
        self._client = client

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
        return any(bool(getattr(client, attr, None)) for attr in _CREDENTIAL_ATTRIBUTES)

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
