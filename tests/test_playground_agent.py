"""Agentic crawler playground: guarded action checklist and per-action results."""

from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.services.playground import PlaygroundRunner
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock
from tests.fakes import FakeTextGenerator


@contextmanager
def _client(
    settings: Settings,
    clock: FakeClock,
    generator: FakeTextGenerator | None,
) -> Iterator[TestClient]:
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock))
    with TestClient(app) as client:
        app.state.container.playground_runner = PlaygroundRunner(
            max_runs_per_hour=20,
            http_factory=lambda _base: nullcontext(client),
            sleep=lambda seconds: clock.advance(seconds),
            ai_generator=generator,
        )
        yield client


def _settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={
            "demo_public": True,
            "dashboard_public": True,
            "max_requests_per_session": 4,
            "rate_limit_requests": 3,
            "rate_limit_window_seconds": 1.0,
        }
    )


def _wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        body = client.get(f"/playground/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.03)
    raise AssertionError(f"playground run {run_id} did not finish")


def _start(client: TestClient, action_ids: list[str]) -> dict[str, Any]:
    response = client.post("/playground/runs", json={"action_ids": action_ids})
    assert response.status_code == 202, response.text
    return _wait_for_run(client, response.json()["run_id"])


@pytest.fixture
def agent_client(test_settings: Settings, fake_clock: FakeClock) -> Iterator[tuple[TestClient, FakeTextGenerator]]:
    generator = FakeTextGenerator(
        tool_script=[
            [("run_action", {"action_id": "allowed_page"})],
            [("run_action", {"action_id": "disallowed_path"})],
            "The allowed page was served and refunded. The disallowed path was blocked and its deposit was kept.",
        ]
    )
    with _client(_settings(test_settings), fake_clock, generator) as client:
        yield client, generator


def test_actions_endpoint_reports_agent_availability(agent_client: tuple[TestClient, FakeTextGenerator]) -> None:
    client, _ = agent_client
    body = client.get("/playground/actions").json()
    assert body["ai"]["available"] is True
    assert body["ai"]["driver"] == "agent"
    assert body["ai"]["model"] == "claude-test-model"
    assert body["max_actions"] == 6
    assert {"allowed_page", "disallowed_path"} <= set(body["default_action_ids"])


def test_agent_runs_selected_checklist_with_fresh_sessions(agent_client: tuple[TestClient, FakeTextGenerator]) -> None:
    client, generator = agent_client
    run = _start(client, ["allowed_page", "disallowed_path"])

    assert run["status"] == "completed"
    assert run["outcome"] == "mixed"
    assert run["driver"] == "agent"
    assert run["selected_action_ids"] == ["allowed_page", "disallowed_path"]
    assert [result["action_id"] for result in run["action_results"]] == [
        "allowed_page",
        "disallowed_path",
    ]

    allowed, refused = run["action_results"]
    assert allowed["outcome"] == "allowed"
    assert allowed["deposit"]["deposit_state"] == "REFUNDED"
    assert "Sentinel News" in (allowed["rendered_html"] or "") or "Top Headlines" in (allowed["rendered_html"] or "")

    assert refused["outcome"] == "refused"
    assert refused["violation"]["reason_code"] == "DISALLOWED_PATH"
    assert refused["deposit"]["deposit_state"] == "FORFEITED"

    # Each action made its own challenge/deposit/session and both appear in the timeline.
    assert len({result["session_id"] for result in run["action_results"]}) == 2
    assert any(step["action_id"] == "allowed_page" for step in run["steps"])
    assert any(step["action_id"] == "disallowed_path" for step in run["steps"])
    assert run["agent"]["status"] == "completed"
    assert "deposit was kept" in run["agent"]["summary"]
    assert generator.tool_loops and generator.tool_loops[0]["tools"] == ["run_action"]


def test_agent_cannot_run_an_unselected_action(test_settings: Settings, fake_clock: FakeClock) -> None:
    generator = FakeTextGenerator(
        tool_script=[
            [("run_action", {"action_id": "rate_limit_burst"})],
            "Only the selected action should be available.",
        ]
    )
    with _client(_settings(test_settings), fake_clock, generator) as client:
        run = _start(client, ["allowed_page"])

    assert [result["action_id"] for result in run["action_results"]] == ["allowed_page"]
    assert run["action_results"][0]["outcome"] == "allowed"
    first_result = generator.tool_loops[0]["results"][0]
    assert first_result["is_error"] is True
    assert "not in the selected checklist" in first_result["content"]


def test_server_completes_actions_the_agent_skips(test_settings: Settings, fake_clock: FakeClock) -> None:
    generator = FakeTextGenerator(
        tool_script=[
            [("run_action", {"action_id": "allowed_page"})],
            "The agent stopped early.",
        ]
    )
    with _client(_settings(test_settings), fake_clock, generator) as client:
        run = _start(client, ["allowed_page", "disallowed_path"])

    assert run["status"] == "completed"
    assert [result["action_id"] for result in run["action_results"]] == [
        "allowed_page",
        "disallowed_path",
    ]
    assert any("remaining selected actions" in (step["message"] or "") for step in run["steps"])


def test_checklist_is_disabled_without_an_ai_key(test_settings: Settings, fake_clock: FakeClock) -> None:
    with _client(_settings(test_settings), fake_clock, None) as client:
        body = client.get("/playground/actions").json()
        assert body["ai"]["available"] is False
        response = client.post(
            "/playground/runs",
            json={"action_ids": ["allowed_page"]},
        )
    assert response.status_code == 503
    assert response.json()["error"] == "playground_ai_unavailable"


def test_checklist_rejects_too_many_or_duplicate_actions(
    agent_client: tuple[TestClient, FakeTextGenerator],
) -> None:
    client, _ = agent_client
    too_many = [
        "allowed_page",
        "deposit_and_crawl",
        "polite_pace",
        "valid_paid_token",
        "disallowed_path",
        "rate_limit_burst",
        "budget_overrun",
    ]
    response = client.post("/playground/runs", json={"action_ids": too_many})
    assert response.status_code == 400

    duplicate = client.post(
        "/playground/runs",
        json={"action_ids": ["allowed_page", "allowed_page"]},
    )
    assert duplicate.status_code == 400


def test_agent_run_records_each_action_in_the_audit_log(
    agent_client: tuple[TestClient, FakeTextGenerator], test_settings: Settings
) -> None:
    client, _ = agent_client
    run = _start(client, ["allowed_page", "disallowed_path"])
    events = client.get(
        "/audit/events",
        params={"event_type": "PLAYGROUND_ACTION_FINISHED"},
        headers={"X-Admin-Token": test_settings.admin_api_token},
    ).json()
    by_run = [event for event in events if event["details"].get("playground_run_id") == run["run_id"]]
    assert {event["details"]["action_id"] for event in by_run} == {"allowed_page", "disallowed_path"}
    assert {event["allowed"] for event in by_run} == {True, False}
