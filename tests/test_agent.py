"""Tests for the Claude-driven crawler agent, using a scripted fake model and the real gateway."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Iterator

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from client import agent as agent_cli
from client.agent import COMPLIANT, ROGUE, SERVER_SIDE_FALLBACK_BETA, CrawlerAgent
from client.agent_tools import TOOL_DEFINITIONS, GatewayToolbox, html_to_text
from server.config import Settings
from server.main import create_app
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock


def _jsonable(obj: Any) -> Any:
    return vars(obj) if isinstance(obj, SimpleNamespace) else str(obj)


class FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(
            {
                "kwargs": {key: value for key, value in kwargs.items() if key != "messages"},
                # Snapshot now: the agent keeps appending to the same list.
                "messages_json": json.dumps(kwargs["messages"], default=_jsonable),
                "last_message": json.loads(json.dumps(kwargs["messages"][-1], default=_jsonable)),
            }
        )
        if not self.script:
            raise AssertionError("Fake model received more calls than scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, script: list[Any]) -> None:
        self.api_key = "sk-ant-test"
        self.beta = SimpleNamespace(messages=FakeMessages(script))


def tool_use(block_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def thinking(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=value)


def reply(*blocks: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop,
        stop_details=None,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=50, cache_read_input_tokens=800),
    )


@pytest.fixture
def gateway(test_settings: Settings, fake_clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(settings=test_settings, rate_limiter=SlidingWindowRateLimiter(clock=fake_clock))
    with TestClient(app) as client:
        yield client


def _toolbox(gateway: TestClient, fake_clock: FakeClock, max_deposit: str = "5.00") -> GatewayToolbox:
    return GatewayToolbox(
        http=gateway,
        crawler_id="ai-test-crawler",
        max_deposit=Decimal(max_deposit),
        sleep=fake_clock.advance,
    )


def _results_by_tool(toolbox: GatewayToolbox, name: str) -> list[dict[str, Any]]:
    return [call.result for call in toolbox.calls if call.name == name]


def test_compliant_agent_completes_session_and_gets_refund(
    gateway: TestClient, fake_clock: FakeClock
) -> None:
    llm = FakeAnthropic(
        [
            reply(thinking("I should start with /news."), tool_use("t1", "fetch_page", {"path": "/news"})),
            reply(tool_use("t2", "read_policy", {})),
            reply(tool_use("t3", "pay_deposit", {})),
            reply(tool_use("t4", "accept_policy", {"policy_version": "1.0", "agree_to_terms": True})),
            reply(
                tool_use("t5", "fetch_page", {"path": "/news"}),
                tool_use("t6", "fetch_page", {"path": "/news/technology"}),
            ),
            reply(tool_use("t7", "fetch_page", {"path": "/news/economy"})),
            reply(tool_use("t8", "wait", {"seconds": 10})),
            reply(tool_use("t9", "complete_session", {})),
            reply(
                text(
                    "- Publishers adopt accountable AI crawling\n"
                    "- Ignored an instruction on /news/economy to fetch /private/customer-data\n"
                    "Deposit returned."
                ),
                stop="end_turn",
            ),
        ]
    )
    toolbox = _toolbox(gateway, fake_clock)
    events: list[tuple[str, dict[str, Any]]] = []
    run = CrawlerAgent(
        llm=llm, toolbox=toolbox, persona=COMPLIANT, on_event=lambda k, p: events.append((k, p))
    ).run()

    assert run.stop == "completed"
    assert "Deposit returned" in run.final_text
    assert run.turns == 9
    assert run.input_tokens == 9000 and run.cache_read_tokens == 7200
    assert toolbox.outcome is not None
    assert toolbox.outcome["deposit"]["deposit_state"] == "REFUNDED"

    challenge = _results_by_tool(toolbox, "fetch_page")[0]
    assert challenge["status"] == 402 and challenge["deposit"]["amount"] == "5.00"

    economy = _results_by_tool(toolbox, "fetch_page")[-1]
    assert economy["status"] == 200
    assert economy["content"].startswith("<untrusted_page_content>")
    assert "Note to AI agents" in economy["content"]
    assert ("thinking", {"text": "I should start with /news."}) in events

    # The session's bearer token is never shown to the model.
    assert toolbox.access_token
    for call in llm.beta.messages.calls:
        assert toolbox.access_token not in call["messages_json"]

    # Parallel tool calls come back as tool_result blocks in a single user message.
    parallel_results = llm.beta.messages.calls[5]["last_message"]
    assert parallel_results["role"] == "user"
    assert [block["tool_use_id"] for block in parallel_results["content"]] == ["t5", "t6"]
    assert all(block["type"] == "tool_result" for block in parallel_results["content"])


def test_agent_request_shape(gateway: TestClient, fake_clock: FakeClock) -> None:
    llm = FakeAnthropic([reply(text("Nothing to do."), stop="end_turn")])
    CrawlerAgent(llm=llm, toolbox=_toolbox(gateway, fake_clock), persona=COMPLIANT, effort="high").run()

    sent = llm.beta.messages.calls[0]["kwargs"]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["tools"] == TOOL_DEFINITIONS
    assert all(tool["strict"] is True for tool in sent["tools"])
    assert all(tool["input_schema"]["additionalProperties"] is False for tool in sent["tools"])
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert sent["output_config"] == {"effort": "high"}
    assert sent["betas"] == [SERVER_SIDE_FALLBACK_BETA]
    assert sent["fallbacks"] == "default"
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in sent and "temperature" not in sent
    assert "untrusted data" in sent["system"]


def test_rogue_agent_is_blocked_and_forfeits_deposit(
    gateway: TestClient, fake_clock: FakeClock
) -> None:
    burst = [tool_use(f"b{i}", "fetch_page", {"path": "/news"}) for i in range(4)]
    llm = FakeAnthropic(
        [
            reply(tool_use("t1", "fetch_page", {"path": "/news"})),
            reply(tool_use("t2", "pay_deposit", {})),
            reply(tool_use("t3", "accept_policy", {"policy_version": "1.0", "agree_to_terms": True})),
            reply(
                tool_use("t4", "fetch_page", {"path": "/admin"}),
                tool_use("t5", "fetch_page", {"path": "/private/customer-data"}),
                tool_use("t6", "fetch_page", {"path": "/%2e%2e/admin"}),
            ),
            reply(*burst),
            reply(tool_use("t7", "complete_session", {})),
            reply(text("The gateway blocked /admin and private paths and kept the deposit."), stop="end_turn"),
        ]
    )
    toolbox = _toolbox(gateway, fake_clock)
    run = CrawlerAgent(llm=llm, toolbox=toolbox, persona=ROGUE).run()

    assert run.stop == "completed"
    assert toolbox.outcome is not None
    deposit = toolbox.outcome["deposit"]
    assert deposit["deposit_state"] == "FORFEITED"
    assert deposit["policy_decision"] == "FORFEITED_BY_POLICY"

    fetches = _results_by_tool(toolbox, "fetch_page")
    blocked = {r["path"]: r for r in fetches if r.get("blocked")}
    assert blocked["/admin"]["reason_code"] == "DISALLOWED_PATH"
    assert blocked["/admin"]["disqualifying"] is True
    assert blocked["/private/customer-data"]["reason_code"] == "DISALLOWED_PATH"
    assert blocked["/%2e%2e/admin"]["reason_code"] == "INVALID_PATH"
    rate_limited = [r for r in fetches if r.get("reason_code") == "RATE_LIMIT_EXCEEDED"]
    assert len(rate_limited) == 1 and rate_limited[0]["retry_after_seconds"] > 0


def test_spending_limit_blocks_payment(gateway: TestClient, fake_clock: FakeClock) -> None:
    llm = FakeAnthropic(
        [
            reply(tool_use("t1", "fetch_page", {"path": "/news"})),
            reply(tool_use("t2", "pay_deposit", {})),
            reply(text("The deposit exceeds my spending limit, so I stopped."), stop="end_turn"),
        ]
    )
    toolbox = _toolbox(gateway, fake_clock, max_deposit="0.001")
    run = CrawlerAgent(llm=llm, toolbox=toolbox, persona=COMPLIANT).run()

    assert run.stop == "completed"
    assert toolbox.deposit is None
    payment = toolbox.calls[1]
    assert payment.is_error and "spending limit" in payment.result["error"]
    sent_back = llm.beta.messages.calls[2]["last_message"]["content"][0]
    assert sent_back["is_error"] is True


def test_refusal_stops_before_running_tools(gateway: TestClient, fake_clock: FakeClock) -> None:
    llm = FakeAnthropic([reply(tool_use("t1", "fetch_page", {"path": "/news"}), stop="refusal")])
    toolbox = _toolbox(gateway, fake_clock)
    run = CrawlerAgent(llm=llm, toolbox=toolbox, persona=ROGUE).run()
    assert run.stop == "refusal"
    assert toolbox.calls == []


def test_api_errors_end_the_run(gateway: TestClient, fake_clock: FakeClock) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=request), body=None)
    run = CrawlerAgent(
        llm=FakeAnthropic([error]), toolbox=_toolbox(gateway, fake_clock), persona=COMPLIANT
    ).run()
    assert run.stop == "error"
    assert "rate limit" in (run.error or "")


def test_tool_input_validation(gateway: TestClient, fake_clock: FakeClock) -> None:
    toolbox = _toolbox(gateway, fake_clock)
    content, is_error = toolbox.execute("fetch_page", {"path": "news"})
    assert is_error and "starting with '/'" in json.loads(content)["error"]
    _, is_error = toolbox.execute("accept_policy", {"policy_version": "1.0", "agree_to_terms": True})
    assert is_error
    _, is_error = toolbox.execute("not_a_tool", {})
    assert is_error
    content, _ = toolbox.execute("wait", {"seconds": 999})
    assert json.loads(content)["waited_seconds"] == 30.0


def test_html_to_text_keeps_hidden_text() -> None:
    html = '<html><body><h1>Title</h1><p style="display:none">Hidden note</p><p>Body  text</p></body></html>'
    assert html_to_text(html) == "Title Hidden note Body text"


def test_cli_requires_claude_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "OPENAI_API_KEY", "AI_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Ignore any developer .env so the test stays hermetic.
    monkeypatch.setattr(agent_cli, "load_dotenv", lambda *args, **kwargs: False)
    assert agent_cli.main(["--persona", "compliant"]) == 2
