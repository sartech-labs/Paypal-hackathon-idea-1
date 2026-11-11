"""Curated crawler playground actions against the live gateway."""

from __future__ import annotations

import time
from contextlib import nullcontext
from decimal import Decimal
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from server.services.playground import PlaygroundRunner
from server.services.rate_limiter import SlidingWindowRateLimiter
from tests.conftest import FakeClock


def _make_client(settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock))
    with TestClient(app) as client:
        app.state.container.playground_runner = PlaygroundRunner(
            max_runs_per_hour=20,
            http_factory=lambda _base: nullcontext(client),
            sleep=lambda seconds: clock.advance(seconds),
        )
        yield client


def _run(client: TestClient, action_id: str) -> dict[str, Any]:
    response = client.post("/playground/runs", json={"action_id": action_id})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        body = client.get(f"/playground/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.03)
    raise AssertionError(f"playground run {run_id} did not finish")


@pytest.fixture
def playground_client(test_settings: Settings, fake_clock: FakeClock) -> Iterator[TestClient]:
    settings = test_settings.model_copy(
        update={
            "demo_public": True,
            "dashboard_public": True,
            # Keep the budget/rate actions deterministic and fast.
            "max_requests_per_session": 4,
            "rate_limit_requests": 3,
            "rate_limit_window_seconds": 1.0,
        }
    )
    yield from _make_client(settings, fake_clock)


def test_actions_list_is_curated(playground_client: TestClient) -> None:
    body = playground_client.get("/playground/actions").json()
    ids = {action["action_id"] for action in body["actions"]}
    assert {"allowed_page", "deposit_and_crawl", "polite_pace", "valid_paid_token"} <= ids
    assert {"disallowed_path", "rate_limit_burst", "budget_overrun", "no_deposit", "path_traversal", "forged_signature"} <= ids


def test_allowed_page_returns_html_and_refunds(playground_client: TestClient) -> None:
    run = _run(playground_client, "allowed_page")
    assert run["status"] == "completed" and run["outcome"] == "allowed"
    assert "Sentinel News" in (run["rendered_html"] or "") or "Top Headlines" in (run["rendered_html"] or "")
    assert run["deposit"]["deposit_state"] == "REFUNDED"
    assert run["audit_event_ids"]


def test_disallowed_path_forfeits_the_deposit(playground_client: TestClient) -> None:
    run = _run(playground_client, "disallowed_path")
    assert run["outcome"] == "refused"
    assert run["violation"]["http_status"] == 403
    assert run["violation"]["reason_code"] == "DISALLOWED_PATH"
    assert run["violation"]["rule_matched"] == "Disallow: /admin"
    assert run["deposit"]["deposit_state"] == "FORFEITED"


def test_no_deposit_and_forged_signature_leave_no_deposit(playground_client: TestClient) -> None:
    for action_id, status_code, reason in (
        ("no_deposit", 402, "PAYMENT_REQUIRED"),
        ("forged_signature", 401, "INVALID_SIGNATURE"),
    ):
        run = _run(playground_client, action_id)
        assert run["outcome"] == "refused"
        assert run["violation"]["http_status"] == status_code
        assert run["violation"]["reason_code"] == reason
        assert run["deposit"]["deposit_state"] is None


def test_path_traversal_and_budget_overrun(playground_client: TestClient) -> None:
    traversal = _run(playground_client, "path_traversal")
    assert traversal["violation"]["reason_code"] == "INVALID_PATH"
    assert traversal["deposit"]["deposit_state"] == "FORFEITED"

    overrun = _run(playground_client, "budget_overrun")
    assert overrun["violation"]["reason_code"] == "MAX_REQUESTS_EXCEEDED"
    assert overrun["deposit"]["deposit_state"] == "FORFEITED"


# --- live-policy page selection, pacing, deadline, audit and detail semantics -------------

import json
import inspect
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from server.api import playground as playground_api
from server.services import playground as playground_service
from server.services.playground import (
    DEFAULT_PAGE_PATH,
    DEFAULT_RUN_TIMEOUT_SECONDS,
    MAX_INTER_REQUEST_PAUSE_SECONDS,
    _pace_seconds,
)

DATA_DIR = Path("data")


def _site_files(tmp_path: Path, *, allowed_paths: list[str], pages: dict[str, str], pricing_tiers: Any = None) -> dict[str, Any]:
    """Write a throwaway site config + allowed pages, for non-bundled-site tests."""
    config = json.loads((DATA_DIR / "site_config.json").read_text(encoding="utf-8"))
    config["allowed_paths"] = allowed_paths
    config["pricing_tiers"] = pricing_tiers if pricing_tiers is not None else []
    site_config_path = tmp_path / "site_config.json"
    site_config_path.write_text(json.dumps(config), encoding="utf-8")
    page_data = {
        path: {
            "title": title,
            "content_type": "text/html; charset=utf-8",
            "category": "general",
            "body": f"<html><body><h1>{title}</h1></body></html>",
        }
        for path, title in pages.items()
    }
    allowed_pages_path = tmp_path / "allowed_pages.json"
    allowed_pages_path.write_text(json.dumps(page_data), encoding="utf-8")
    return {"site_config_path": site_config_path, "allowed_pages_path": allowed_pages_path}


@contextmanager
def _playground_client(
    test_settings: Settings,
    clock: FakeClock,
    *,
    runner_factory: Optional[Callable[[TestClient], PlaygroundRunner]] = None,
    **overrides: Any,
) -> Iterator[TestClient]:
    settings = test_settings.model_copy(
        update={
            "demo_public": True,
            "dashboard_public": True,
            "max_requests_per_session": 4,
            "rate_limit_requests": 3,
            "rate_limit_window_seconds": 1.0,
            **overrides,
        }
    )
    app = create_app(settings=settings, rate_limiter=SlidingWindowRateLimiter(clock=clock))
    with TestClient(app) as client:
        if runner_factory is not None:
            runner = runner_factory(client)
        else:
            runner = PlaygroundRunner(
                max_runs_per_hour=20,
                http_factory=lambda _base: nullcontext(client),
                sleep=lambda seconds: clock.advance(seconds),
            )
        app.state.container.playground_runner = runner
        yield client


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any], headers: Optional[dict[str, str]] = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)


    def json(self) -> dict[str, Any]:
        return self._body


class _FakeGateway:
    """Minimal stand-in for the gateway HTTP surface used by the paid-session handshake."""

    base_url = "http://testserver"

    def __init__(self, challenge_path: str) -> None:
        self.challenge_path = challenge_path

    def __enter__(self) -> "_FakeGateway":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        paid = bool((kwargs.get("headers") or {}).get("Authorization"))
        if url == self.challenge_path and not paid:
            return _FakeResponse(
                402,
                {"challenge_id": "ch_1", "crawler_id": "crawl_1", "policy_version": "1.0", "path": url},
            )
        if url.startswith("/deposits/"):
            return _FakeResponse(
                200,
                {
                    "deposit_id": "dep_1",
                    "required_amount": "1.00",
                    "currency": "USD",
                    "deposit_state": "REFUNDED",
                    "payment_status": "captured",
                    "policy_decision": "REFUNDED",
                },
            )
        # Authorized by the gateway (SARA headers present) but the origin has no page.
        return _FakeResponse(404, {"error": "not_found"}, headers={"X-SARA-Session-Id": "ses_1"})

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if url == "/deposits/create":
            return _FakeResponse(201, {"deposit_id": "dep_1", "required_amount": "1.00", "currency": "USD"})
        if url == "/sessions/accept-policy":
            return _FakeResponse(201, {"session_id": "ses_1", "access_token": "tok_1"})
        return _FakeResponse(200, {"status": "ok"})


class _RedirectingGateway(_FakeGateway):
    """An origin that redirects `/articles` to `/articles/` with an absolute URL."""

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        paid = bool((kwargs.get("headers") or {}).get("Authorization"))
        if url == self.challenge_path and not paid:
            return super().get(url, **kwargs)
        if url == self.challenge_path:
            return _FakeResponse(
                308,
                {"error": "moved"},
                headers={"location": "http://origin.example/articles/"},
            )
        if url == "/articles/":
            return _FakeResponse(200, {"title": "Articles"}, headers={"X-SARA-Session-Id": "ses_1"})
        return super().get(url, **kwargs)


def _wait_for_run(run: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run.finished:
            return True
        time.sleep(0.01)
    return False


def test_action_pages_are_derived_from_the_live_policy(
    test_settings: Settings, fake_clock: FakeClock, tmp_path: Path
) -> None:
    files = _site_files(
        tmp_path,
        allowed_paths=["/", "/articles", "/blog"],
        pages={"/": "Home", "/articles": "Articles", "/blog": "Blog"},
    )
    with _playground_client(test_settings, fake_clock, **files) as client:
        run = _run(client, "allowed_page")
    assert run["status"] == "completed", run.get("error")
    assert run["outcome"] == "allowed"
    requests = [step["request"] for step in run["steps"] if step["request"]]
    assert "GET /articles" in requests
    assert not any("/news" in request for request in requests)
    assert "Articles" in (run["rendered_html"] or "")
    assert run["deposit"]["deposit_state"] == "REFUNDED"


def test_tier_resources_are_used_when_allowed_paths_only_group_tiers(
    test_settings: Settings, fake_clock: FakeClock, tmp_path: Path
) -> None:
    tiers = json.loads((DATA_DIR / "site_config.json").read_text(encoding="utf-8"))["pricing_tiers"]
    pages = {path: entry["title"] for path, entry in json.loads((DATA_DIR / "allowed_pages.json").read_text()).items()}
    files = _site_files(tmp_path, allowed_paths=["/", "/content"], pages=pages, pricing_tiers=tiers)
    with _playground_client(test_settings, fake_clock, **files, rate_limit_requests=5) as client:
        run = _run(client, "allowed_page")
    assert run["status"] == "completed", run.get("error")
    assert run["outcome"] == "allowed"
    requests = [step["request"] for step in run["steps"] if step["request"]]
    assert "GET /content/premium/market-intelligence-report" in requests
    assert not any("/news" in request for request in requests)
    assert "Market Intelligence Report" in (run["rendered_html"] or "")


def test_origin_404_for_an_allowed_path_degrades_with_a_clear_message() -> None:
    policy = json.loads((DATA_DIR / "site_config.json").read_text(encoding="utf-8"))
    policy["allowed_paths"] = ["/", "/articles", "/blog"]
    policy["pricing_tiers"] = []
    gateway = _FakeGateway(challenge_path="/articles")
    runner = PlaygroundRunner(max_runs_per_hour=5, http_factory=lambda _base: gateway, sleep=lambda _s: None)
    run = runner.start(action_id="allowed_page", base_url="http://testserver", policy=policy)
    assert _wait_for_run(run)
    assert run.status == "completed", run.error
    assert run.outcome == "allowed"
    assert run.violation is None
    assert run.deposit is not None and run.deposit["deposit_state"] == "REFUNDED"
    messages = [step.message or "" for step in run.steps]
    assert any("origin returned HTTP 404" in message for message in messages)
    assert any("No page published at the allowed paths" in step.label for step in run.steps)


def test_allowed_page_follows_origin_redirect_through_gateway() -> None:
    policy = json.loads((DATA_DIR / "site_config.json").read_text(encoding="utf-8"))
    policy["allowed_paths"] = ["/", "/articles"]
    policy["pricing_tiers"] = []
    gateway = _RedirectingGateway(challenge_path="/articles")
    runner = PlaygroundRunner(max_runs_per_hour=5, http_factory=lambda _base: gateway, sleep=lambda _s: None)
    run = runner.start(action_id="allowed_page", base_url="http://testserver", policy=policy)
    assert _wait_for_run(run)
    assert run.status == "completed", run.error
    assert run.outcome == "allowed"
    assert run.violation is None
    assert run.deposit is not None and run.deposit["deposit_state"] == "REFUNDED"
    requests = [step.request for step in run.steps if step.request]
    assert "GET /articles" in requests
    assert "GET /articles/" in requests
    assert "Articles" in (run.rendered_html or "")


def test_slow_rate_limit_pauses_are_capped() -> None:
    assert _pace_seconds({"rate_limit_window_seconds": 3600, "rate_limit_requests": 1}) <= MAX_INTER_REQUEST_PAUSE_SECONDS
    assert _pace_seconds({"rate_limit_window_seconds": 10, "rate_limit_requests": 3}) <= MAX_INTER_REQUEST_PAUSE_SECONDS


def test_polite_pace_does_not_wait_out_a_long_rate_limit_window(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    slept: list[float] = []

    def factory(client: TestClient) -> PlaygroundRunner:
        def sleep(seconds: float) -> None:
            slept.append(seconds)
            fake_clock.advance(seconds)

        return PlaygroundRunner(
            max_runs_per_hour=20,
            http_factory=lambda _base: nullcontext(client),
            sleep=sleep,
        )

    with _playground_client(
        test_settings,
        fake_clock,
        runner_factory=factory,
        rate_limit_requests=1,
        rate_limit_window_seconds=3600.0,
        max_requests_per_session=3,
    ) as client:
        run = _run(client, "polite_pace")
    assert run["outcome"] == "allowed", run.get("violation")
    assert sum(slept) <= 2 * MAX_INTER_REQUEST_PAUSE_SECONDS


class _ClockAdvancingGateway(_FakeGateway):
    """Fake gateway that consumes wall-clock time on every call."""

    def __init__(self, challenge_path: str, clock: FakeClock, step_seconds: float = 5.0) -> None:
        super().__init__(challenge_path)
        self._clock = clock
        self._step_seconds = step_seconds

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._clock.advance(self._step_seconds)
        return super().get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._clock.advance(self._step_seconds)
        return super().post(url, **kwargs)


def test_run_stops_at_the_hard_deadline_instead_of_hanging() -> None:
    assert DEFAULT_RUN_TIMEOUT_SECONDS == 60.0
    clock = FakeClock()
    policy = json.loads((DATA_DIR / "site_config.json").read_text(encoding="utf-8"))
    gateway = _ClockAdvancingGateway(challenge_path="/news", clock=clock)
    runner = PlaygroundRunner(
        max_runs_per_hour=5,
        http_factory=lambda _base: gateway,
        sleep=lambda seconds: clock.advance(seconds),
        clock=clock,
        run_timeout_seconds=1.0,
    )
    run = runner.start(action_id="allowed_page", base_url="http://testserver", policy=policy)
    assert _wait_for_run(run)
    assert run.status == "failed"
    assert run.outcome == "error"
    assert "time limit" in (run.error or "")
    assert any("time limit" in (step.message or "") for step in run.steps)


def test_refused_runs_are_audited_as_not_allowed(playground_client: TestClient, test_settings: Settings) -> None:
    headers = {"X-Admin-Token": test_settings.admin_api_token}
    refused = _run(playground_client, "disallowed_path")
    allowed = _run(playground_client, "allowed_page")
    events = playground_client.get(
        "/audit/events", params={"event_type": "PLAYGROUND_RUN_FINISHED"}, headers=headers
    ).json()
    by_run = {event["details"].get("playground_run_id"): event for event in events}
    refused_event = by_run[refused["run_id"]]
    allowed_event = by_run[allowed["run_id"]]
    assert refused_event["allowed"] is False
    assert refused_event["reason_code"] == "DISALLOWED_PATH"
    assert allowed_event["allowed"] is True
    assert allowed_event["reason_code"] == "ALLOWED"


def test_burst_reports_the_disqualifying_violation(playground_client: TestClient) -> None:
    run = _run(playground_client, "rate_limit_burst")
    assert run["outcome"] == "refused"
    assert run["deposit"]["deposit_state"] == "FORFEITED"
    assert run["violation"]["reason_code"] == "RATE_LIMIT_EXCEEDED"
    assert run["violation"]["disqualifying"] is True


def test_valid_paid_token_step_carries_the_session_headers(playground_client: TestClient) -> None:
    run = _run(playground_client, "valid_paid_token")
    assert run["outcome"] == "allowed"
    paid_steps = [step for step in run["steps"] if step["request"] == "GET /news/technology"]
    assert paid_steps
    step = paid_steps[-1]
    assert step["http_status"] == 200
    headers = step["detail"].get("response_headers") or {}
    assert headers.get("X-SARA-Session-Id") == run["session_id"]
    assert "X-SARA-Remaining-Budget" in headers
    assert "remaining budget" in (step["message"] or "").lower()


def test_playground_dead_code_is_gone() -> None:
    assert not hasattr(playground_service.PlaygroundRunner, "_finish_allowed")
    assert 'access == "none"' not in inspect.getsource(playground_api.list_actions)
    assert DEFAULT_PAGE_PATH == "/news"


def test_disallowed_path_action_degrades_when_the_site_forbids_nothing() -> None:
    policy = json.loads((DATA_DIR / "site_config.json").read_text(encoding="utf-8"))
    policy["disallowed_paths"] = []
    gateway = _FakeGateway(challenge_path="/news")
    runner = PlaygroundRunner(max_runs_per_hour=5, http_factory=lambda _base: gateway, sleep=lambda _s: None)
    run = runner.start(action_id="disallowed_path", base_url="http://testserver", policy=policy)
    assert _wait_for_run(run)
    assert run.status == "failed"
    assert "no disallowed paths" in (run.error or "")


def test_deposit_and_crawl_stays_inside_a_small_session_budget(
    test_settings: Settings, fake_clock: FakeClock
) -> None:
    with _playground_client(
        test_settings, fake_clock, max_requests_per_session=2, rate_limit_requests=3, rate_limit_window_seconds=1.0
    ) as client:
        run = _run(client, "deposit_and_crawl")
    assert run["outcome"] == "allowed", run.get("violation")
    assert run["deposit"]["deposit_state"] == "REFUNDED"
    reads = [step for step in run["steps"] if (step["label"] or "").startswith("Read allowed page")]
    assert len(reads) == 2
    assert any("allows 2 request(s) per session" in (step["message"] or "") for step in run["steps"])
