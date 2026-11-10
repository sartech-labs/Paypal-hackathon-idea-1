"""Tests for the live demo page, the demo runner, and the scripted crawler driver."""

from __future__ import annotations

import time
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from client.agent import CrawlerAgent, PERSONAS
from client.agent_tools import GatewayToolbox
from client.scripted_agent import ScriptedDriver
from server.config import Settings
from server.main import create_app
from server.services.ai_client import AIServiceError
from server.services.demo_runner import DemoRunner
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock
from tests.fakes import FakeTextGenerator

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}


def _make_client(settings: Settings, fake_clock: FakeClock, *, ai_available: bool = False, llm: Any = None, max_runs: int = 20) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        rate_limiter=SlidingWindowRateLimiter(clock=fake_clock),
        # Explanations fall back to the rule-based template (the fake model has no replies).
        ai_generator=FakeTextGenerator(
            [AIServiceError("no scripted explanation")] * 4 if ai_available else None,
            available=ai_available,
            provider="claude",
            model="claude-opus-5-5",
        ),
    )
    with TestClient(app) as client:
        # Runs call this gateway through the same test client; waits advance the fake clock.
        app.state.container.demo_runner = DemoRunner(
            max_runs_per_hour=max_runs,
            http_factory=lambda _base: nullcontext(client),
            llm_factory=lambda _provider: llm,
            sleep=fake_clock.advance,
        )
        yield client


@pytest.fixture
def demo_client(test_settings: Settings, fake_clock: FakeClock) -> Iterator[TestClient]:
    yield from _make_client(test_settings, fake_clock)


def _wait_for(client: TestClient, run_id: str, headers: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/demo/runs/{run_id}", headers=headers).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def _results(run: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [e["data"]["result"] for e in run["events"] if e["kind"] == "tool_result" and e["data"]["name"] == name]


# --- pages ---------------------------------------------------------------------------------


def test_demo_page_console_and_assets_are_served(demo_client: TestClient) -> None:
    page = demo_client.get("/crawler")
    assert page.status_code == 200 and "SARA Live Demo" in page.text
    landing = demo_client.get("/")
    assert landing.status_code == 200 and "SARA publisher console" in landing.text
    assert "SARA Agent Studio" in demo_client.get("/agent").text
    assert demo_client.get("/demo").status_code == 200
    assert "SARA publisher console" in demo_client.get("/console").text
    for asset in ("/_sara/static/demo.js", "/_sara/static/demo.css", "/_sara/static/paypal_panel.js"):
        assert demo_client.get(asset).status_code == 200


def test_paypal_callback_renders_html_for_browsers(demo_client: TestClient) -> None:
    resp = demo_client.get("/deposits/paypal/cancel?deposit_id=dep_x&token=ORDER", headers={"Accept": "text/html"})
    # Unknown deposits still fail as JSON; the HTML page is only for real callbacks.
    assert resp.status_code == 404


# --- access control and guards -----------------------------------------------------------


def test_private_demo_requires_admin_token(demo_client: TestClient) -> None:
    config = demo_client.get("/demo/config").json()
    assert config["access"] == "none" and not config["can_start_runs"]
    assert demo_client.post("/demo/runs", json={"driver": "scripted", "persona": "compliant"}).status_code == 401
    assert demo_client.get("/demo/config", headers={"X-Admin-Token": "wrong"}).status_code == 401
    admin = demo_client.get("/demo/config", headers=ADMIN).json()
    assert admin["access"] == "admin" and admin["can_start_runs"]
    assert admin["payment_mode"] == "mock" and admin["paypal_autopay"] is False


def test_ai_runs_need_an_ai_key(demo_client: TestClient) -> None:
    resp = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "ai", "persona": "compliant"})
    assert resp.status_code == 503 and resp.json()["error"] == "demo_ai_unavailable"


def test_public_demo_runs_are_capped_per_hour(test_settings: Settings, fake_clock: FakeClock) -> None:
    settings = test_settings.model_copy(update={"demo_public": True})
    for client in _make_client(settings, fake_clock, max_runs=1):
        assert client.get("/demo/config").json()["access"] == "public"
        first = client.post("/demo/runs", json={"driver": "scripted", "persona": "rogue"})
        assert first.status_code == 202
        _wait_for(client, first.json()["run_id"], {})
        second = client.post("/demo/runs", json={"driver": "scripted", "persona": "rogue"})
        assert second.status_code == 429


# --- scripted runs end to end ----------------------------------------------------------------


def test_scripted_compliant_run_gets_its_deposit_back(demo_client: TestClient) -> None:
    started = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "compliant"})
    assert started.status_code == 202
    run = _wait_for(demo_client, started.json()["run_id"], ADMIN)

    assert run["status"] == "completed" and run["error"] is None
    assert run["deposit"]["deposit_state"] == "REFUNDED"
    assert run["session"]["total_violations"] == 0
    assert run["explanation"]["deposit_state"] == "REFUNDED"
    pages = _results(run, "fetch_page")
    assert pages[0]["status"] == 402
    assert [p["status"] for p in pages[1:]] == [200, 200, 200, 200]
    assert any(p.get("prompt_injection") for p in pages)
    # Page text reaches the page as a short preview without the untrusted-content tags.
    assert all("<untrusted_page_content>" not in str(p.get("content", "")) for p in pages)
    assert _results(run, "wait"), "the crawler should pace itself to the rate limit"
    assert "Ignored a hidden instruction" in run["final_text"]
    kinds = [e["kind"] for e in run["events"]]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"

    config = demo_client.get("/demo/config", headers=ADMIN).json()
    assert config["active_run_id"] is None
    assert config["recent_runs"][0]["deposit_state"] == "REFUNDED"


def test_scripted_rogue_run_forfeits_its_deposit(demo_client: TestClient) -> None:
    started = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "rogue"})
    run = _wait_for(demo_client, started.json()["run_id"], ADMIN)
    assert run["deposit"]["deposit_state"] == "FORFEITED"
    blocked = [p for p in _results(run, "fetch_page") if p.get("blocked")]
    assert {p["reason_code"] for p in blocked} >= {"DISALLOWED_PATH", "INVALID_PATH", "RATE_LIMIT_EXCEEDED"}
    assert not _results(run, "read_policy")
    assert run["explanation"]["deposit_state"] == "FORFEITED"


def test_events_can_be_fetched_incrementally(demo_client: TestClient) -> None:
    started = demo_client.post("/demo/runs", headers=ADMIN, json={"driver": "scripted", "persona": "rogue"})
    run = _wait_for(demo_client, started.json()["run_id"], ADMIN)
    tail = demo_client.get(f"/demo/runs/{run['run_id']}?after={run['next_seq'] - 2}", headers=ADMIN).json()
    assert [e["seq"] for e in tail["events"]] == [run["next_seq"] - 2, run["next_seq"] - 1]
    assert demo_client.get("/demo/runs/run_missing", headers=ADMIN).status_code == 404


# --- AI run through the runner ----------------------------------------------------------------


def _reply(*blocks: SimpleNamespace, stop: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, stop_details=None, model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=50))


def _use(block_id: str, name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


class _ScriptedClaude:
    def __init__(self, script: list[Any]) -> None:
        self.api_key = "sk-ant-test"
        calls: list[dict[str, Any]] = []
        self.calls = calls

        def create(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return script.pop(0)

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_ai_run_uses_the_gateways_model(test_settings: Settings, fake_clock: FakeClock) -> None:
    llm = _ScriptedClaude(
        [
            _reply(_use("t1", "fetch_page", {"path": "/news"})),
            _reply(_use("t2", "pay_deposit", {})),
            _reply(_use("t3", "accept_policy", {"policy_version": "1.0", "agree_to_terms": True})),
            _reply(_use("t4", "fetch_page", {"path": "/news"})),
            _reply(_use("t5", "complete_session", {})),
            _reply(SimpleNamespace(type="text", text="Brief: one page read. Deposit refunded."), stop="end_turn"),
        ]
    )
    for client in _make_client(test_settings, fake_clock, ai_available=True, llm=llm):
        started = client.post("/demo/runs", headers=ADMIN, json={"driver": "ai", "persona": "compliant"})
        assert started.status_code == 202
        run = _wait_for(client, started.json()["run_id"], ADMIN)
        assert run["status"] == "completed"
        assert run["provider"] == "anthropic" and run["model"] == "claude-opus-5-5"
        assert run["deposit"]["deposit_state"] == "REFUNDED"
        assert run["final_text"] == "Brief: one page read. Deposit refunded."
        assert run["usage"]["turns"] == 6 and run["usage"]["input_tokens"] == 600
        assert llm.calls[0]["model"] == "claude-opus-5-5"


# --- cancellation and the scripted driver ---------------------------------------------------


def test_cancelled_agent_stops_before_the_next_turn() -> None:
    toolbox = GatewayToolbox(http=None, crawler_id="c", max_deposit=__import__("decimal").Decimal("1"))  # type: ignore[arg-type]
    agent = CrawlerAgent(llm=None, toolbox=toolbox, persona=PERSONAS["compliant"], provider="scripted", driver=ScriptedDriver("compliant"), should_stop=lambda: True)
    result = agent.run()
    assert result.stop == "cancelled" and result.turns == 0


def test_scripted_driver_follows_the_challenge_and_policy() -> None:
    driver = ScriptedDriver("compliant")
    first = driver.next_turn()
    assert first.stop == "tool_use" and first.tool_calls[0].name == "fetch_page"
    driver.add_tool_results([(first.tool_calls[0], '{"status": 200}', False)])
    final = driver.next_turn()
    assert final.stop == "end_turn" and "Expected an HTTP 402" in final.text
    with pytest.raises(ValueError):
        ScriptedDriver("unknown")
