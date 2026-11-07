"""Tests for the OpenAI provider and provider selection."""

from __future__ import annotations

from typing import Any

import httpx
import httpx2
import openai
import pytest

from server.config import Settings
from server.main import create_app
from server.services import ai_client
from server.services.ai_client import (
    AIRefusalError,
    AIServiceError,
    AIUnavailableError,
    ClaudeTextGenerator,
    OpenAITextGenerator,
    build_text_generator,
    resolve_ai_provider,
)
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow
from tests.fakes import FakeAsyncOpenAI, FakeTextGenerator, fake_openai_response

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}


def _openai(test_settings: Settings, client: FakeAsyncOpenAI, **overrides: Any) -> OpenAITextGenerator:
    settings = test_settings.model_copy(update={"ai_enabled": True, "ai_provider": "openai", **overrides})
    return OpenAITextGenerator(settings, client=client)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_openai_request_uses_responses_api_with_strict_schema(test_settings: Settings) -> None:
    client = FakeAsyncOpenAI(response=fake_openai_response('{"ok": true}'))
    generator = _openai(test_settings, client)
    assert generator.provider == "openai"
    assert generator.model == "gpt-5.5"

    result = await generator.generate(system="sys", prompt="hello", json_schema={"type": "object"})
    assert result.json() == {"ok": True}
    assert result.model == "gpt-5.5-2026-04-23"

    sent = client.responses.calls[0]
    assert sent["model"] == "gpt-5.5"
    assert sent["instructions"] == "sys"
    assert sent["input"] == "hello"
    assert sent["reasoning"] == {"effort": "medium"}
    assert sent["max_output_tokens"] == 16000
    assert sent["text"] == {
        "format": {"type": "json_schema", "name": "sara_output", "schema": {"type": "object"}, "strict": True}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("effort", "expected"), [("low", "low"), ("high", "high"), ("xhigh", "high"), ("max", "high")])
async def test_openai_effort_mapping(test_settings: Settings, effort: str, expected: str) -> None:
    client = FakeAsyncOpenAI(response=fake_openai_response("text"))
    await _openai(test_settings, client, ai_effort=effort).generate(system="s", prompt="p")
    assert client.responses.calls[0]["reasoning"] == {"effort": expected}
    assert "text" not in client.responses.calls[0]


@pytest.mark.asyncio
async def test_openai_model_override(test_settings: Settings) -> None:
    client = FakeAsyncOpenAI(response=fake_openai_response("text"))
    generator = _openai(test_settings, client, ai_model="gpt-5.4-mini")
    await generator.generate(system="s", prompt="p")
    assert generator.model == "gpt-5.4-mini"
    assert client.responses.calls[0]["model"] == "gpt-5.4-mini"


@pytest.mark.asyncio
async def test_openai_refusal_and_incomplete_responses(test_settings: Settings) -> None:
    refused = FakeAsyncOpenAI(response=fake_openai_response(refusal="I can't help with that."))
    with pytest.raises(AIRefusalError, match="can't help"):
        await _openai(test_settings, refused).generate(system="s", prompt="p")

    truncated = FakeAsyncOpenAI(response=fake_openai_response("{", status="incomplete", incomplete_reason="max_output_tokens"))
    with pytest.raises(AIServiceError, match="cut off"):
        await _openai(test_settings, truncated).generate(system="s", prompt="p")

    filtered = FakeAsyncOpenAI(response=fake_openai_response("", status="incomplete", incomplete_reason="content_filter"))
    with pytest.raises(AIRefusalError, match="content filter"):
        await _openai(test_settings, filtered).generate(system="s", prompt="p")

    empty = FakeAsyncOpenAI(response=fake_openai_response("   "))
    with pytest.raises(AIServiceError, match="empty"):
        await _openai(test_settings, empty).generate(system="s", prompt="p")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_type", "message"),
    [
        ("auth", AIUnavailableError, "rejected"),
        ("not_found", AIServiceError, "not found"),
        ("rate", AIServiceError, "rate limit"),
        ("server", AIServiceError, "HTTP 500"),
        ("connection", AIServiceError, "Could not reach"),
    ],
)
async def test_openai_error_mapping(test_settings: Settings, error: str, expected_type: type, message: str) -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    errors: dict[str, Exception] = {
        "auth": openai.AuthenticationError("bad key", response=httpx2.Response(401, request=request), body=None),
        "not_found": openai.NotFoundError("no model", response=httpx2.Response(404, request=request), body=None),
        "rate": openai.RateLimitError("slow down", response=httpx2.Response(429, request=request), body=None),
        "server": openai.InternalServerError("boom", response=httpx2.Response(500, request=request), body=None),
        "connection": openai.APIConnectionError(request=request),
    }
    with pytest.raises(expected_type, match=message):
        await _openai(test_settings, FakeAsyncOpenAI(error=errors[error])).generate(system="s", prompt="p")


@pytest.mark.asyncio
async def test_openai_unavailable_without_key(test_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    generator = OpenAITextGenerator(test_settings.model_copy(update={"ai_enabled": True}))
    assert generator.is_available() is False
    with pytest.raises(AIUnavailableError, match="OPENAI_API_KEY"):
        await generator.generate(system="s", prompt="p")

    no_key_client = FakeAsyncOpenAI(response=fake_openai_response("x"), api_key="")
    assert _openai(test_settings, no_key_client).is_available() is False


def test_provider_selection(test_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = test_settings.model_copy(update={"ai_provider": "openai"})
    assert resolve_ai_provider(explicit) == "openai"
    assert isinstance(build_text_generator(explicit), OpenAITextGenerator)
    assert resolve_ai_provider(test_settings) == "anthropic"
    assert isinstance(build_text_generator(test_settings), ClaudeTextGenerator)

    auto = test_settings.model_copy(update={"ai_provider": "auto"})
    monkeypatch.setattr(ai_client, "_anthropic_credentials_available", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    assert resolve_ai_provider(auto) == "openai"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert resolve_ai_provider(auto) == "anthropic"
    monkeypatch.setattr(ai_client, "_anthropic_credentials_available", lambda: True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    assert resolve_ai_provider(auto) == "anthropic"


def test_model_defaults_per_provider(test_settings: Settings) -> None:
    assert ClaudeTextGenerator(test_settings).model == "claude-opus-5-5"
    assert OpenAITextGenerator(test_settings, client=FakeAsyncOpenAI()).model == "gpt-5.5"  # type: ignore[arg-type]
    assert Settings(ai_model="  ").ai_model is None


@pytest.mark.asyncio
async def test_explanations_and_drafts_record_the_openai_provider(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    generator = FakeTextGenerator(provider="openai", model="gpt-5.5-2026-04-23")
    app = create_app(
        settings=test_settings,
        rate_limiter=SlidingWindowRateLimiter(clock=fake_clock),
        ai_generator=generator,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/health")).json()["ai"]["provider"] == "openai"
        assert (await client.get("/dashboard/data", headers=ADMIN)).json()["ai_provider"] == "openai"

        _, deposit, session = await complete_onboarding_flow(client, "crawler-openai")
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        assert (await client.get("/gateway/admin", headers=headers)).status_code == 403
        assert (await client.post("/sessions/complete", headers=headers)).status_code == 200

        generator.responses = [
            {"headline": "Kept", "summary": "Forbidden path.", "findings": [{"statement": "Requested /admin.", "evidence": ["E1"]}]},
            {
                "site_id": "demo-news-site", "site_name": "Demo", "policy_version": "1.1", "deposit_amount": "0.25",
                "currency": "USD", "max_requests_per_session": 20, "rate_limit_requests": 5,
                "rate_limit_window_seconds": 10, "session_ttl_seconds": 1800,
                "allowed_paths": ["/", "/news"], "disallowed_paths": ["/admin"],
                "refund_policy": {
                    "description": "Refund if compliant.", "disqualify_on_disallowed_path": True,
                    "max_rate_limit_violations_before_disqualification": 2,
                    "max_budget_violations_before_disqualification": 1,
                    "require_explicit_session_completion": True,
                },
                "assumptions": [], "warnings": [],
            },
        ]
        explanation = (await client.get(f"/deposits/{deposit['deposit_id']}/explanation", headers=headers)).json()
        assert explanation["generated_by"] == "openai"
        assert explanation["model"] == "gpt-5.5-2026-04-23"

        draft = (
            await client.post(
                "/policies/draft",
                json={"terms": "Crawlers may read /news but never /admin; 5 requests per 10 seconds."},
                headers=ADMIN,
            )
        ).json()
        assert draft["generated_by"] == "openai" and draft["valid"] is True
