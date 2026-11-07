"""Tests for the crawler agent on the OpenAI Responses API, with a scripted fake client."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from client.agent import COMPLIANT, ROGUE, CrawlerAgent
from client.agent_tools import TOOL_DEFINITIONS, GatewayToolbox
from server.config import Settings
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock


def _jsonable(obj: Any) -> Any:
    return vars(obj) if isinstance(obj, SimpleNamespace) else str(obj)


class FakeResponses:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(
            {
                "kwargs": {key: value for key, value in kwargs.items() if key != "input"},
                # Snapshot now: the driver keeps appending to the same list.
                "input_json": json.dumps(kwargs["input"], default=_jsonable),
                "input_len": len(kwargs["input"]),
                "last_items": json.loads(json.dumps(kwargs["input"][-3:], default=_jsonable)),
            }
        )
        if not self.script:
            raise AssertionError("Fake OpenAI received more calls than scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeOpenAI:
    def __init__(self, script: list[Any]) -> None:
        self.api_key = "sk-test-openai"
        self.responses = FakeResponses(script)


_counter = {"n": 0}


def call(name: str, arguments: Any) -> SimpleNamespace:
    _counter["n"] += 1
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(type="function_call", id=f"fc_{_counter['n']}", call_id=f"call_{_counter['n']}", name=name, arguments=raw)


def reasoning(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="reasoning", summary=[SimpleNamespace(type="summary_text", text=text)])


def message(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)])


def response(*items: SimpleNamespace, status: str = "completed", reason: Optional[str] = None) -> SimpleNamespace:
    text = "".join(part.text for item in items if item.type == "message" for part in item.content if part.type == "output_text")
    return SimpleNamespace(
        status=status,
        model="gpt-5.5-2026-04-23",
        output=list(items),
        output_text=text,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        usage=SimpleNamespace(input_tokens=500, output_tokens=40, input_tokens_details=SimpleNamespace(cached_tokens=300)),
    )


@pytest.fixture
def gateway(test_settings: Settings, fake_clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(settings=test_settings, rate_limiter=SlidingWindowRateLimiter(clock=fake_clock))
    with TestClient(app) as client:
        yield client


def _toolbox(gateway: TestClient, fake_clock: FakeClock) -> GatewayToolbox:
    return GatewayToolbox(http=gateway, crawler_id="ai-openai-crawler", max_deposit=Decimal("5.00"), sleep=fake_clock.advance)


def _agent(llm: FakeOpenAI, toolbox: GatewayToolbox, persona: Any = COMPLIANT, **kwargs: Any) -> CrawlerAgent:
    return CrawlerAgent(llm=llm, toolbox=toolbox, persona=persona, provider="openai", **kwargs)


def test_openai_compliant_agent_gets_refund(gateway: TestClient, fake_clock: FakeClock) -> None:
    llm = FakeOpenAI(
        [
            response(reasoning("Start with /news to see the challenge."), call("fetch_page", {"path": "/news"})),
            response(call("read_policy", {})),
            response(call("pay_deposit", {})),
            response(call("accept_policy", {"policy_version": "1.0", "agree_to_terms": True})),
            response(call("fetch_page", {"path": "/news"}), call("fetch_page", {"path": "/news/economy"})),
            response(call("complete_session", {})),
            response(message("- Publishers adopt accountable crawling\n- Ignored a hidden instruction\nDeposit returned.")),
        ]
    )
    toolbox = _toolbox(gateway, fake_clock)
    events: list[tuple[str, dict[str, Any]]] = []
    run = _agent(llm, toolbox, on_event=lambda k, p: events.append((k, p))).run()

    assert run.stop == "completed"
    assert "Deposit returned" in run.final_text
    assert run.turns == 7 and run.input_tokens == 3500 and run.cache_read_tokens == 2100
    assert run.model == "gpt-5.5-2026-04-23"
    assert toolbox.outcome is not None and toolbox.outcome["deposit"]["deposit_state"] == "REFUNDED"
    assert ("thinking", {"text": "Start with /news to see the challenge."}) in events

    first = llm.responses.calls[0]["kwargs"]
    assert first["model"] == "gpt-5.5"
    assert first["instructions"] == COMPLIANT.system
    assert first["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert [tool["name"] for tool in first["tools"]] == [tool["name"] for tool in TOOL_DEFINITIONS]
    for tool in first["tools"]:
        assert tool["type"] == "function" and tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False
    assert "tool_choice" not in first

    # The two parallel calls come back as two function_call_output items with their call_ids.
    parallel = llm.responses.calls[5]["last_items"]
    outputs = [item for item in parallel if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert len(outputs) == 2 and all(item["call_id"].startswith("call_") for item in outputs)

    assert toolbox.access_token
    for sent in llm.responses.calls:
        assert toolbox.access_token not in sent["input_json"]


def test_openai_rogue_agent_forfeits(gateway: TestClient, fake_clock: FakeClock) -> None:
    llm = FakeOpenAI(
        [
            response(call("fetch_page", {"path": "/news"})),
            response(call("pay_deposit", {})),
            response(call("accept_policy", {"policy_version": "1.0", "agree_to_terms": True})),
            response(call("fetch_page", {"path": "/admin"}), call("fetch_page", {"path": "/private/customer-data"})),
            response(call("complete_session", {})),
            response(message("Blocked on /admin and private paths; deposit kept.")),
        ]
    )
    toolbox = _toolbox(gateway, fake_clock)
    run = _agent(llm, toolbox, persona=ROGUE, model="gpt-5.4-mini").run()
    assert run.stop == "completed"
    assert llm.responses.calls[0]["kwargs"]["model"] == "gpt-5.4-mini"
    assert toolbox.outcome is not None and toolbox.outcome["deposit"]["deposit_state"] == "FORFEITED"


def test_openai_refusal_and_truncation_stop_the_run(gateway: TestClient, fake_clock: FakeClock) -> None:
    refused = response(SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="I can't do that.")]))
    toolbox = _toolbox(gateway, fake_clock)
    run = _agent(FakeOpenAI([refused]), toolbox).run()
    assert run.stop == "refusal" and "can't do that" in (run.error or "")
    assert toolbox.calls == []

    truncated = response(status="incomplete", reason="max_output_tokens")
    run = _agent(FakeOpenAI([truncated]), _toolbox(gateway, fake_clock)).run()
    assert run.stop == "max_tokens"


def test_openai_invalid_arguments_are_reported_to_the_model(gateway: TestClient, fake_clock: FakeClock) -> None:
    llm = FakeOpenAI([response(call("fetch_page", "{not json")), response(message("Stopping."))])
    toolbox = _toolbox(gateway, fake_clock)
    run = _agent(llm, toolbox).run()
    assert run.stop == "completed"
    assert toolbox.calls[0].is_error
    returned = [item for item in llm.responses.calls[1]["last_items"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert "Invalid input" in returned[0]["output"]


def test_openai_api_errors_end_the_run(gateway: TestClient, fake_clock: FakeClock) -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    error = openai.RateLimitError("quota", response=httpx2.Response(429, request=request), body=None)
    run = _agent(FakeOpenAI([error]), _toolbox(gateway, fake_clock)).run()
    assert run.stop == "error" and "rate limit" in (run.error or "")
