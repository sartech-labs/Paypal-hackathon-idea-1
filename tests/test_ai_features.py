"""Tests for the Claude-backed explainer, policy assistant, and Claude client wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator

import anthropic
import httpx
import httpx2
import pytest

from server.config import Settings
from server.main import create_app
from server.services.ai_client import (
    SERVER_SIDE_FALLBACK_BETA,
    AIRefusalError,
    AIServiceError,
    AIUnavailableError,
    ClaudeTextGenerator,
)
from server.services.explainer import EXPLANATION_SCHEMA
from server.services.policy_assistant import POLICY_DRAFT_SCHEMA
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock, complete_onboarding_flow
from tests.fakes import FakeAsyncAnthropic, FakeTextGenerator, fake_message

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}


def _client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def fake_generator() -> FakeTextGenerator:
    return FakeTextGenerator()


@pytest.fixture
async def ai_client(
    test_settings: Settings,
    fake_clock: FakeClock,
    fake_generator: FakeTextGenerator,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        settings=test_settings,
        rate_limiter=SlidingWindowRateLimiter(clock=fake_clock),
        ai_generator=fake_generator,
    )
    async with _client_for(app) as client:
        yield client


async def _forfeited_session(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    _, deposit, session = await complete_onboarding_flow(client, "crawler-rogue-ai")
    headers = {"Authorization": f"Bearer {session['access_token']}"}
    assert (await client.get("/gateway/news", headers=headers)).status_code == 200
    assert (await client.get("/gateway/admin", headers=headers)).status_code == 403
    completed = await client.post("/sessions/complete", headers=headers)
    assert completed.json()["deposit"]["deposit_state"] == "FORFEITED"
    return deposit["deposit_id"], headers


# --- Explainer -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explanation_falls_back_to_template_when_ai_disabled(
    async_client: httpx.AsyncClient,
) -> None:
    deposit_id, headers = await _forfeited_session(async_client)

    resp = await async_client.get(f"/deposits/{deposit_id}/explanation", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_by"] == "template"
    assert body["deposit_state"] == "FORFEITED"
    assert "disabled" in body["notice"]
    assert body["headline"].startswith("Deposit kept after")

    evidence_ids = {item["evidence_id"] for item in body["evidence"]}
    assert body["findings"], "template explanation should contain findings"
    for finding in body["findings"]:
        assert finding["evidence"] and set(finding["evidence"]) <= evidence_ids
    assert any("/admin" in finding["statement"] for finding in body["findings"])


@pytest.mark.asyncio
async def test_claude_explanation_keeps_only_valid_citations(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    deposit_id, headers = await _forfeited_session(ai_client)
    fake_generator.responses = [
        {
            "headline": "Deposit kept: crawler requested a forbidden admin page",
            "summary": "The crawler requested /admin, which the policy forbids.",
            "findings": [
                {"statement": "Requested /admin, which is disallowed.", "evidence": ["E4", "E999"]},
                {"statement": "Unsupported claim.", "evidence": ["E999"]},
            ],
        }
    ]

    resp = await ai_client.get(f"/deposits/{deposit_id}/explanation", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_by"] == "claude"
    assert body["model"] == "claude-test-model"
    assert body["findings"] == [
        {"statement": "Requested /admin, which is disallowed.", "evidence": ["E4"]}
    ]

    call = fake_generator.calls[0]
    assert call["json_schema"] == EXPLANATION_SCHEMA
    assert "strictly as data" in call["system"]
    assert "<evidence>" in call["prompt"] and '"/admin"' in call["prompt"]
    assert "FORFEITED" in call["prompt"]


@pytest.mark.asyncio
async def test_claude_explanation_without_valid_citations_uses_template(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    deposit_id, headers = await _forfeited_session(ai_client)
    fake_generator.responses = [
        {"headline": "x", "summary": "y", "findings": [{"statement": "z", "evidence": ["E404"]}]}
    ]

    body = (await ai_client.get(f"/deposits/{deposit_id}/explanation", headers=headers)).json()
    assert body["generated_by"] == "template"
    assert "did not cite any valid evidence" in body["notice"]


@pytest.mark.asyncio
async def test_crawler_cannot_trigger_ai_before_outcome_is_final(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    _, deposit, session = await complete_onboarding_flow(ai_client, "crawler-in-progress")
    headers = {"Authorization": f"Bearer {session['access_token']}"}

    body = (
        await ai_client.get(f"/deposits/{deposit['deposit_id']}/explanation", headers=headers)
    ).json()
    assert body["generated_by"] == "template"
    assert "once the deposit outcome is final" in body["notice"]
    assert fake_generator.calls == []

    fake_generator.responses = [
        {"headline": "In progress", "summary": "Not final.", "findings": [{"statement": "Paid.", "evidence": ["E1"]}]}
    ]
    admin_body = (
        await ai_client.get(f"/deposits/{deposit['deposit_id']}/explanation", headers=ADMIN)
    ).json()
    assert admin_body["generated_by"] == "claude"
    assert len(fake_generator.calls) == 1


@pytest.mark.asyncio
async def test_explanation_is_cached_per_deposit_state(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    deposit_id, headers = await _forfeited_session(ai_client)
    fake_generator.responses = [
        {"headline": "h", "summary": "s", "findings": [{"statement": "f", "evidence": ["E1"]}]}
    ]
    first = (await ai_client.get(f"/deposits/{deposit_id}/explanation", headers=headers)).json()
    second = (await ai_client.get(f"/deposits/{deposit_id}/explanation", headers=headers)).json()
    assert first["generated_by"] == second["generated_by"] == "claude"
    assert len(fake_generator.calls) == 1


@pytest.mark.asyncio
async def test_explanation_requires_owner_or_admin(async_client: httpx.AsyncClient) -> None:
    deposit_id, _ = await _forfeited_session(async_client)
    _, _, other = await complete_onboarding_flow(async_client, "crawler-other")

    assert (await async_client.get(f"/deposits/{deposit_id}/explanation")).status_code == 401
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await async_client.get(f"/deposits/{deposit_id}/explanation", headers=other_headers)
    assert resp.status_code == 403
    assert (await async_client.get(f"/deposits/{deposit_id}/explanation", headers=ADMIN)).status_code == 200
    assert (await async_client.get("/deposits/dep_missing/explanation", headers=ADMIN)).status_code == 404


# --- Policy assistant ----------------------------------------------------------------

TERMS = (
    "Crawlers may read our homepage and the news section. The admin area and the "
    "members area are off limits. Allow 5 requests every 10 seconds and charge a 50 cent deposit."
)


def _valid_draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "site_id": "demo-news-site",
        "site_name": "CrawlerTrust Sentinel News",
        "policy_version": "1.1",
        "deposit_amount": "0.5",
        "currency": "usd",
        "max_requests_per_session": 10,
        "rate_limit_requests": 5,
        "rate_limit_window_seconds": 10,
        "session_ttl_seconds": 1800,
        "allowed_paths": ["/", "/news", "news//"],
        "disallowed_paths": ["/admin", "/members"],
        "refund_policy": {
            "description": "Refunded when no disqualifying violations occur.",
            "disqualify_on_disallowed_path": True,
            "max_rate_limit_violations_before_disqualification": 2,
            "max_budget_violations_before_disqualification": 1,
            "require_explicit_session_completion": True,
        },
        "assumptions": ["Kept the current session TTL of 1800 seconds."],
        "warnings": [],
    }
    draft.update(overrides)
    return draft


@pytest.mark.asyncio
async def test_policy_draft_requires_admin(ai_client: httpx.AsyncClient) -> None:
    resp = await ai_client.post("/policies/draft", json={"terms": TERMS})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_policy_draft_returns_validated_normalized_draft(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    fake_generator.responses = [_valid_draft()]
    resp = await ai_client.post("/policies/draft", json={"terms": TERMS}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True, body["validation_errors"]
    assert body["draft"]["deposit_amount"] == "0.50"
    assert body["draft"]["currency"] == "USD"
    assert body["draft"]["allowed_paths"] == ["/", "/news"]
    assert body["draft"]["challenge_ttl_seconds"] == 600
    assert body["assumptions"] == ["Kept the current session TTL of 1800 seconds."]
    assert "Draft only" in body["notice"]

    call = fake_generator.calls[0]
    assert call["json_schema"] == POLICY_DRAFT_SCHEMA
    assert "<publisher_terms>" in call["prompt"] and "50 cent deposit" in call["prompt"]


@pytest.mark.asyncio
async def test_policy_draft_reports_validation_errors(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    fake_generator.responses = [
        _valid_draft(
            deposit_amount="500",
            allowed_paths=["/news", "../etc"],
            disallowed_paths=["/news"],
            rate_limit_requests=0,
        )
    ]
    body = (await ai_client.post("/policies/draft", json={"terms": TERMS}, headers=ADMIN)).json()
    assert body["valid"] is False
    errors = " ".join(body["validation_errors"])
    assert "deposit_amount" in errors
    assert "both allowed and disallowed" in errors
    assert "rate_limit_requests" in errors
    assert "not a valid path rule" in errors


@pytest.mark.asyncio
async def test_policy_draft_maps_ai_errors(
    ai_client: httpx.AsyncClient,
    fake_generator: FakeTextGenerator,
) -> None:
    fake_generator.responses = [
        AIUnavailableError("No Claude credentials are configured."),
        AIServiceError("Could not reach the Claude API."),
        "not json",
    ]
    first = await ai_client.post("/policies/draft", json={"terms": TERMS}, headers=ADMIN)
    assert first.status_code == 503 and first.json()["error"] == "ai_unavailable"
    second = await ai_client.post("/policies/draft", json={"terms": TERMS}, headers=ADMIN)
    assert second.status_code == 502 and second.json()["error"] == "ai_error"
    third = await ai_client.post("/policies/draft", json={"terms": TERMS}, headers=ADMIN)
    assert third.status_code == 502


@pytest.mark.asyncio
async def test_health_reports_ai_status(
    ai_client: httpx.AsyncClient, async_client: httpx.AsyncClient
) -> None:
    ai = (await ai_client.get("/health")).json()["ai"]
    assert ai == {"enabled": False, "available": True, "provider": "claude", "model": "claude-test-model"}
    default_ai = (await async_client.get("/health")).json()["ai"]
    assert default_ai["available"] is False
    assert default_ai["provider"] == "claude"
    assert default_ai["model"] == "claude-opus-5-5"


# --- Claude client wrapper -----------------------------------------------------------


def _generator(test_settings: Settings, client: FakeAsyncAnthropic, **overrides: Any) -> ClaudeTextGenerator:
    settings = test_settings.model_copy(update={"ai_enabled": True, **overrides})
    return ClaudeTextGenerator(settings, client=client)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_generator_sends_structured_request_with_refusal_fallback(test_settings: Settings) -> None:
    client = FakeAsyncAnthropic(response=fake_message('{"ok": true}'))
    generator = _generator(test_settings, client, ai_effort="high")

    result = await generator.generate(system="sys", prompt="hello", json_schema={"type": "object"})
    assert result.json() == {"ok": True}
    assert result.model == "claude-opus-5-5"

    sent = client.beta.messages.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "hello"}]
    assert sent["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert sent["betas"] == [SERVER_SIDE_FALLBACK_BETA]
    assert sent["fallbacks"] == "default"
    assert "thinking" not in sent and "temperature" not in sent and "tool_choice" not in sent


@pytest.mark.asyncio
async def test_generator_can_disable_refusal_fallback(test_settings: Settings) -> None:
    client = FakeAsyncAnthropic(response=fake_message("plain text"))
    generator = _generator(test_settings, client, ai_refusal_fallback=False)
    assert (await generator.generate(system="s", prompt="p")).text == "plain text"
    sent = client.beta.messages.calls[0]
    assert "betas" not in sent and "fallbacks" not in sent
    assert sent["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_generator_handles_refusal_and_truncation(test_settings: Settings) -> None:
    refused = fake_message("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(AIRefusalError, match="cyber"):
        await _generator(test_settings, FakeAsyncAnthropic(response=refused)).generate(system="s", prompt="p")

    truncated = fake_message('{"partial": ', stop_reason="max_tokens")
    with pytest.raises(AIServiceError, match="cut off"):
        await _generator(test_settings, FakeAsyncAnthropic(response=truncated)).generate(system="s", prompt="p")


@pytest.mark.asyncio
async def test_generator_reports_unavailable_without_credentials(test_settings: Settings) -> None:
    no_key = FakeAsyncAnthropic(response=fake_message("x"), api_key=None)
    generator = _generator(test_settings, no_key)
    assert generator.is_available() is False
    with pytest.raises(AIUnavailableError, match="ANTHROPIC_API_KEY"):
        await generator.generate(system="s", prompt="p")
    assert no_key.beta.messages.calls == []

    empty_key = _generator(test_settings, FakeAsyncAnthropic(response=fake_message("x"), api_key=""))
    assert empty_key.is_available() is False

    disabled = ClaudeTextGenerator(test_settings, client=FakeAsyncAnthropic())  # type: ignore[arg-type]
    assert disabled.is_available() is False
    with pytest.raises(AIUnavailableError, match="disabled"):
        await disabled.generate(system="s", prompt="p")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_type", "message"),
    [
        ("auth", AIUnavailableError, "rejected"),
        ("rate", AIServiceError, "rate limit"),
        ("server", AIServiceError, "HTTP 500"),
        ("connection", AIServiceError, "Could not reach"),
    ],
)
async def test_generator_maps_sdk_errors(
    test_settings: Settings, error: str, expected_type: type[Exception], message: str
) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    errors: dict[str, Exception] = {
        "auth": anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=request), body=None),
        "rate": anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=request), body=None),
        "server": anthropic.InternalServerError("boom", response=httpx2.Response(500, request=request), body=None),
        "connection": anthropic.APIConnectionError(request=request),
    }
    generator = _generator(test_settings, FakeAsyncAnthropic(error=errors[error]))
    with pytest.raises(expected_type, match=message):
        await generator.generate(system="s", prompt="p")
