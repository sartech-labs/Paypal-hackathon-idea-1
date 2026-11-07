"""Tests for the PayPal MCP client, the PayPal assistant, and its API endpoints."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest

from server.config import Settings
from server.main import create_app
from server.schemas import (
    DepositRecord,
    DepositState,
    PaymentMode,
    PaymentStatus,
    PolicyDecisionStatus,
    RefundTransactionStatus,
    SettlementMode,
)
from server.services.ai_client import (
    ClaudeTextGenerator,
    OpenAITextGenerator,
    ToolSpec,
)
from server.services.paypal_assistant import PayPalAssistant, PayPalAssistantError
from server.services.paypal_mcp import (
    READ_ONLY_TOOLS,
    PayPalAccessTokenSource,
    PayPalMCPClient,
    PayPalMCPError,
    PayPalMCPUnavailableError,
    local_server_command,
    redact,
)
from tests.fakes import (
    FakeAsyncAnthropic,
    FakeAsyncOpenAI,
    FakeMCPRawSession,
    FakeTextGenerator,
    FakeTokenSource,
    fake_mcp_connector,
)

ADMIN = {"X-Admin-Token": "test-admin-secret-token-999"}
DEPOSIT_ID = "dep_0f6528cc53dc40f9b82a34c4b1d9144a"
ORDER_ID = "9XS27667T1928241W"


def _deposit(**changes: Any) -> DepositRecord:
    base: dict[str, Any] = dict(
        deposit_id=DEPOSIT_ID,
        challenge_id="chg_1",
        crawler_id="crawler-1",
        site_id="site",
        policy_version="1.0",
        required_amount="0.01",
        currency="USD",
        payment_mode=PaymentMode.PAYPAL_SANDBOX,
        deposit_state=DepositState.REFUNDED,
        payment_status=PaymentStatus.CAPTURED,
        policy_decision=PolicyDecisionStatus.ELIGIBLE_FOR_REFUND,
        refund_status=RefundTransactionStatus.REFUNDED,
        provider_order_id=ORDER_ID,
        provider_capture_id="3FD790762H6433444",
        provider_refund_id="4EA32173C6959302K",
        provider_refund_status="COMPLETED",
        order_request_id="o",
        capture_request_id="c",
        refund_request_id="r",
        created_at="2026-11-05T06:33:20+00:00",
        updated_at="2026-11-05T06:33:41+00:00",
    )
    base.update(changes)
    return DepositRecord(**base)


def _order(*, capture_status: str = "REFUNDED", authorization: Optional[dict[str, Any]] = None, custom_id: str = DEPOSIT_ID) -> dict[str, Any]:
    payments: dict[str, Any] = {}
    if capture_status:
        payments["captures"] = [{"id": "3FD790762H6433444", "status": capture_status, "amount": {"currency_code": "USD", "value": "0.01"}}]
    if authorization:
        payments["authorizations"] = [authorization]
    return {
        "id": ORDER_ID,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "custom_id": custom_id,
                "amount": {"currency_code": "USD", "value": "0.01"},
                "payee": {"email_address": "sb-merchant@business.example.com"},
                "payments": payments,
            }
        ],
    }


REFUND = {"id": "4EA32173C6959302K", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "0.01"}, "custom_id": DEPOSIT_ID}


def _client(settings: Settings, session: FakeMCPRawSession) -> PayPalMCPClient:
    return PayPalMCPClient(settings, token_source=FakeTokenSource(), connector=fake_mcp_connector(session))  # type: ignore[arg-type]


class _Deposits:
    def __init__(self, records: list[DepositRecord]) -> None:
        self.records = records

    async def list_deposits(self) -> list[DepositRecord]:
        return list(self.records)


# --- MCP client ----------------------------------------------------------------------------


def test_local_server_runs_only_read_only_tools_and_minimal_env(test_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    settings = test_settings.model_copy(update={"paypal_mcp_command": "npx -y @paypal/mcp@1.8.1 --tools=all"})
    command, args, env = local_server_command(settings, "A21-token")
    assert command == "npx"
    assert args[:2] == ["-y", "@paypal/mcp@1.8.1"]
    assert "--tools=all" not in args
    assert args[-1] == "--tools=" + ",".join(sorted(set(READ_ONLY_TOOLS.values())))
    assert env["PAYPAL_ACCESS_TOKEN"] == "A21-token"
    assert env["PAYPAL_ENVIRONMENT"] == "SANDBOX"
    assert env["HTTPS_PROXY"] == "http://proxy.local:3128"
    assert "OPENAI_API_KEY" not in env


@pytest.mark.asyncio
async def test_session_exposes_only_allowlisted_tools_and_blocks_writes(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _order()})
    client = _client(test_settings, raw)
    async with client.open() as session:
        assert sorted(t.name for t in session.tools) == sorted(READ_ONLY_TOOLS)
        blocked = await session.call("create_refund", {"capture_id": "X"})
        result = await session.call("get_order", {"id": ORDER_ID})
    assert not blocked.ok and "not available" in (blocked.error or "")
    assert ("create_refund", {"capture_id": "X"}) not in raw.calls
    assert result.ok and result.data["id"] == ORDER_ID
    assert "sb-merchant@business.example.com" not in result.text
    assert "[email]" in result.text


@pytest.mark.asyncio
async def test_paypal_error_payloads_are_reported_as_failures(test_settings: Settings) -> None:
    raw = FakeMCPRawSession(
        {
            "get_order": {"ok": False, "status": 404, "code": "PAYPAL_API_HTTP_ERROR", "message": "The specified resource does not exist."},
            "list_disputes": {"error": {"message": "not supported", "type": "paypal_error"}},
        }
    )
    async with _client(test_settings, raw).open() as session:
        missing = await session.call("get_order", {"id": "NOTANORDER12345678"})
        unsupported = await session.call("list_disputes", {})
    assert not missing.ok and missing.error == "HTTP 404: The specified resource does not exist."
    assert not unsupported.ok and unsupported.error == "not supported"


@pytest.mark.asyncio
async def test_unconfigured_client_and_setup_errors(test_settings: Settings) -> None:
    disabled = PayPalMCPClient(test_settings.model_copy(update={"paypal_mcp_enabled": False}))
    assert not disabled.is_configured()
    with pytest.raises(PayPalMCPUnavailableError):
        async with disabled.open():
            pass

    no_creds = PayPalMCPClient(test_settings.model_copy(update={"paypal_client_id": "", "paypal_client_secret": ""}))
    assert "PAYPAL_CLIENT_ID" in (no_creds.unavailable_reason() or "")

    from contextlib import asynccontextmanager

    def failing(exc: BaseException) -> Any:
        @asynccontextmanager
        async def connect(_token: str) -> Any:
            raise exc
            yield  # pragma: no cover

        return connect

    missing_node = PayPalMCPClient(test_settings, token_source=FakeTokenSource(), connector=failing(FileNotFoundError("npx")))  # type: ignore[arg-type]
    with pytest.raises(PayPalMCPUnavailableError, match="Node.js"):
        async with missing_node.open():
            pass
    broken = PayPalMCPClient(test_settings, token_source=FakeTokenSource(), connector=failing(ExceptionGroup("tg", [ConnectionError("x")])))  # type: ignore[arg-type]
    with pytest.raises(PayPalMCPError, match="ConnectionError"):
        async with broken.open():
            pass


@pytest.mark.asyncio
async def test_errors_from_the_callers_code_pass_through_unchanged(test_settings: Settings) -> None:
    client = _client(test_settings, FakeMCPRawSession({}))
    with pytest.raises(ValueError, match="caller bug"):
        async with client.open():
            raise ValueError("caller bug")


@pytest.mark.asyncio
async def test_token_source_caches_the_oauth_token(test_settings: Settings) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "A21-live", "expires_in": 32400})

    source = PayPalAccessTokenSource(test_settings, transport=httpx.MockTransport(handler))
    assert await source.get_token() == "A21-live"
    assert await source.get_token() == "A21-live"
    assert len(calls) == 1
    assert calls[0].url.path == "/v1/oauth2/token"


def test_redact_removes_email_addresses() -> None:
    assert redact('{"payer":{"email_address":"buyer@personal.example.com"}}') == '{"payer":{"email_address":"[email]"}}'


# --- deterministic verification ----------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_refunded_deposit_matches_paypal(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _order(), "get_refund": REFUND})
    assistant = PayPalAssistant(FakeTextGenerator(), _client(test_settings, raw), _Deposits([]))
    result = await assistant.verify_deposit(_deposit())
    assert result.consistent
    assert {c.label: c.status for c in result.checks} == {
        "PayPal order": "match",
        "Deposit reference (custom_id)": "match",
        "Amount": "match",
        "Capture": "match",
        "Refund": "match",
    }
    assert [c.tool for c in result.calls] == ["get_order", "get_refund"]
    assert all(c.source == "paypal_mcp" for c in result.calls)
    assert raw.calls == [("get_order", {"id": ORDER_ID}), ("get_refund", {"refund_id": "4EA32173C6959302K"})]


@pytest.mark.asyncio
async def test_verify_flags_a_capture_that_paypal_did_not_refund(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _order(capture_status="COMPLETED"), "get_refund": REFUND})
    assistant = PayPalAssistant(FakeTextGenerator(), _client(test_settings, raw), _Deposits([]))
    result = await assistant.verify_deposit(_deposit())
    assert not result.consistent
    capture = next(c for c in result.checks if c.label == "Capture")
    assert capture.status == "mismatch" and "COMPLETED" in capture.paypal
    assert "disagree" in result.verdict


@pytest.mark.asyncio
async def test_verify_released_authorization_hold(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _order(capture_status="", authorization={"id": "85W91230JP885362X", "status": "VOIDED"})})
    assistant = PayPalAssistant(FakeTextGenerator(), _client(test_settings, raw), _Deposits([]))
    deposit = _deposit(
        settlement_mode=SettlementMode.AUTHORIZATION,
        deposit_state=DepositState.RELEASED,
        payment_status=PaymentStatus.VOIDED,
        refund_status=RefundTransactionStatus.NOT_APPLICABLE_RELEASED,
        provider_capture_id=None,
        provider_refund_id=None,
        provider_refund_status=None,
        provider_authorization_id="85W91230JP885362X",
        provider_authorization_status="VOIDED",
    )
    result = await assistant.verify_deposit(deposit)
    assert result.consistent
    assert [c.label for c in result.checks] == ["PayPal order", "Deposit reference (custom_id)", "Amount", "Authorization hold"]


@pytest.mark.asyncio
async def test_verify_rejects_simulated_deposits(test_settings: Settings) -> None:
    assistant = PayPalAssistant(FakeTextGenerator(), _client(test_settings, FakeMCPRawSession({})), _Deposits([]))
    with pytest.raises(PayPalAssistantError, match="simulated"):
        await assistant.verify_deposit(_deposit(payment_mode=PaymentMode.MOCK))


# --- AI questions --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_uses_gateway_records_and_paypal_mcp_tools(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _order(), "list_disputes": {"items": []}})
    generator = FakeTextGenerator(
        provider="openai",
        model="gpt-5.5",
        tool_script=[
            [("list_sara_deposits", {"limit": 99})],
            [("get_order", {"id": ORDER_ID}), ("list_disputes", {"dispute_state": "NOT_A_STATE"})],
            [("create_refund", {"capture_id": "3FD790762H6433444"})],
            "The last deposit matches PayPal.\n- Order 9XS27667T1928241W: COMPLETED, capture REFUNDED.",
        ],
    )
    assistant = PayPalAssistant(generator, _client(test_settings, raw), _Deposits([_deposit()]))
    answer = await assistant.ask("Check the last deposit against PayPal.")

    loop = generator.tool_loops[0]
    assert loop["tools"] == ["list_sara_deposits", "list_sara_orders", "get_order", "get_refund", "list_transactions", "list_disputes", "get_dispute"]
    assert "<publisher_question>" in loop["prompt"]
    listed = json.loads(loop["results"][0]["content"])
    assert listed["deposits"][0]["paypal_order_id"] == ORDER_ID
    assert loop["results"][1]["content"].startswith('<paypal_mcp_result tool="get_order">')
    assert loop["results"][3]["is_error"] and "Unknown tool" in loop["results"][3]["content"]
    # Invalid dispute states are dropped so PayPal applies its default; writes never reach MCP.
    assert raw.calls == [("get_order", {"id": ORDER_ID}), ("list_disputes", {})]
    assert answer.generated_by == "openai"
    assert [c.tool for c in answer.calls] == ["list_sara_deposits", "get_order", "list_disputes"]
    assert answer.calls[0].source == "sara" and answer.calls[0].arguments == {"limit": 25}
    assert "Read-only" in answer.notice


# --- provider tool loops ------------------------------------------------------------------


def _ai_settings(settings: Settings, provider: str) -> Settings:
    return settings.model_copy(update={"ai_enabled": True, "ai_provider": provider})


@pytest.mark.asyncio
async def test_openai_tool_loop_round_trip(test_settings: Settings) -> None:
    call = SimpleNamespace(type="function_call", call_id="call_1", name="get_order", arguments=json.dumps({"id": ORDER_ID}))
    first = SimpleNamespace(status="completed", model="gpt-5.5", output=[SimpleNamespace(type="reasoning", summary=[]), call], output_text="")
    final = SimpleNamespace(status="completed", model="gpt-5.5", output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="Done.")])], output_text="Done.")
    fake = FakeAsyncOpenAI(response=[first, final])
    generator = OpenAITextGenerator(_ai_settings(test_settings, "openai"), client=fake)  # type: ignore[arg-type]
    executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        executed.append((name, arguments))
        return '{"status": "COMPLETED"}', False

    spec = ToolSpec("get_order", "Look up an order.", {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False})
    result = await generator.run_tool_loop(system="sys", prompt="q", tools=[spec], execute=execute)
    assert result.text == "Done." and result.steps == 2 and result.tool_calls == 1
    assert executed == [("get_order", {"id": ORDER_ID})]
    request = fake.responses.calls[0]
    assert request["tools"][0] == {"type": "function", "name": "get_order", "description": "Look up an order.", "parameters": spec.input_schema, "strict": True}
    second_input = fake.responses.calls[1]["input"]
    assert second_input[-1] == {"type": "function_call_output", "call_id": "call_1", "output": '{"status": "COMPLETED"}'}
    assert call in second_input


@pytest.mark.asyncio
async def test_claude_tool_loop_round_trip(test_settings: Settings) -> None:
    use = SimpleNamespace(type="tool_use", id="toolu_1", name="get_refund", input={"refund_id": "4EA32173C6959302K"})
    first = SimpleNamespace(content=[use], stop_reason="tool_use", stop_details=None, model="claude-opus-5-5")
    final = SimpleNamespace(content=[SimpleNamespace(type="text", text="Refund completed.")], stop_reason="end_turn", stop_details=None, model="claude-opus-5-5")
    fake = FakeAsyncAnthropic(response=[first, final])
    generator = ClaudeTextGenerator(_ai_settings(test_settings, "anthropic"), client=fake)  # type: ignore[arg-type]

    async def execute(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        return json.dumps({"error": "lookup failed"}), True

    spec = ToolSpec("get_refund", "Look up a refund.", {"type": "object", "properties": {"refund_id": {"type": "string"}}, "required": ["refund_id"], "additionalProperties": False})
    result = await generator.run_tool_loop(system="sys", prompt="q", tools=[spec], execute=execute)
    assert result.text == "Refund completed." and result.tool_calls == 1
    request = fake.beta.messages.calls[0]
    assert request["tools"][0]["strict"] is True
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    tool_result = fake.beta.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result == {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"error": "lookup failed"}', "is_error": True}


# --- API -------------------------------------------------------------------------------------


async def _api(settings: Settings, raw: FakeMCPRawSession, generator: Optional[FakeTextGenerator] = None) -> tuple[httpx.AsyncClient, Any]:
    app = create_app(
        settings=settings,
        ai_generator=generator or FakeTextGenerator(available=False),
        paypal_mcp=_client(settings, raw),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    return client, app.state.container


@pytest.mark.asyncio
async def test_api_requires_admin_unless_demo_is_public(test_settings: Settings) -> None:
    client, _ = await _api(test_settings, FakeMCPRawSession({}))
    async with client:
        assert (await client.get("/paypal/assistant/status")).status_code == 401
        status = await client.get("/paypal/assistant/status", headers=ADMIN)
        assert status.status_code == 200
        body = status.json()
        assert body["mcp_configured"] and body["read_only_tools"] == sorted(READ_ONLY_TOOLS)
        assert body["access"] == "admin" and body["questions_remaining_this_hour"] is None

    public, _ = await _api(test_settings.model_copy(update={"demo_public": True}), FakeMCPRawSession({}))
    async with public:
        body = (await public.get("/paypal/assistant/status")).json()
        assert body["access"] == "public" and body["questions_remaining_this_hour"] == 30


@pytest.mark.asyncio
async def test_api_verify_endpoint(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _order(), "get_refund": REFUND})
    client, container = await _api(test_settings, raw)
    async with client:
        assert (await client.post("/paypal/deposits/dep_missing/verify", headers=ADMIN)).status_code == 404
        record = _deposit()
        container.deposit_manager._deposits[record.deposit_id] = record
        ok = await client.post(f"/paypal/deposits/{record.deposit_id}/verify", headers=ADMIN)
        assert ok.status_code == 200 and ok.json()["consistent"] is True
        mock = _deposit(deposit_id="dep_mock", payment_mode=PaymentMode.MOCK)
        container.deposit_manager._deposits[mock.deposit_id] = mock
        conflict = await client.post("/paypal/deposits/dep_mock/verify", headers=ADMIN)
        assert conflict.status_code == 409 and conflict.json()["error"] == "nothing_to_verify"


@pytest.mark.asyncio
async def test_api_ask_needs_ai_and_respects_public_quota(test_settings: Settings) -> None:
    client, _ = await _api(test_settings, FakeMCPRawSession({}))
    async with client:
        resp = await client.post("/paypal/assistant/ask", headers=ADMIN, json={"question": "Any disputes?"})
        assert resp.status_code == 503 and resp.json()["error"] == "ai_unavailable"

    settings = test_settings.model_copy(update={"demo_public": True, "demo_max_questions_per_hour": 1})
    generator = FakeTextGenerator(tool_script=[[("list_disputes", {"dispute_state": None})], "No open disputes."])
    public, _ = await _api(settings, FakeMCPRawSession({"list_disputes": {"items": []}}), generator)
    async with public:
        first = await public.post("/paypal/assistant/ask", json={"question": "Any disputes?"})
        assert first.status_code == 200
        assert first.json()["answer"] == "No open disputes."
        assert first.json()["calls"][0]["summary"] == "0 disputes"
        second = await public.post("/paypal/assistant/ask", json={"question": "Any disputes?"})
        assert second.status_code == 429


@pytest.mark.asyncio
async def test_api_reports_mcp_failures(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"paypal_client_id": "", "paypal_client_secret": ""})
    app = create_app(settings=settings, ai_generator=FakeTextGenerator(available=False))
    container = app.state.container
    record = _deposit()
    container.deposit_manager._deposits[record.deposit_id] = record
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(f"/paypal/deposits/{record.deposit_id}/verify", headers=ADMIN)
        assert resp.status_code == 503 and resp.json()["error"] == "paypal_mcp_unavailable"


# --- commerce orders -------------------------------------------------------------------------


def _commerce_order(**changes: Any) -> Any:
    from server.schemas import CommerceOrderRecord, OrderLine, PaymentMode

    base = dict(
        order_id="ord_abc123", kind="product", agent_id="aria-1", status="PAID", amount="44.99", currency="USD",
        lines=[
            OrderLine(ref="ebook-agent-economy", name="E-book", unit_price="12.00", quantity=1, category="DIGITAL_GOODS"),
            OrderLine(ref="mug-sentinel", name="Mug", unit_price="14.00", quantity=2, category="PHYSICAL_GOODS"),
        ],
        shipping="4.99", payment_mode=PaymentMode.PAYPAL_SANDBOX, simulated=False,
        provider_order_id="ORD-COMMERCE-1", provider_capture_id="CAP-COMMERCE-1", created_at="2026-11-07T10:00:00Z",
    )
    base.update(changes)
    return CommerceOrderRecord(**base)


def _paypal_commerce_order(capture_status: str = "COMPLETED", items: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return {
        "id": "ORD-COMMERCE-1",
        "status": "COMPLETED",
        "purchase_units": [{
            "custom_id": "ord_abc123",
            "amount": {"currency_code": "USD", "value": "44.99"},
            "items": items if items is not None else [{"sku": "ebook-agent-economy", "quantity": "1"}, {"sku": "mug-sentinel", "quantity": "2"}],
            "payments": {"captures": [{"id": "CAP-COMMERCE-1", "status": capture_status}]},
        }],
    }


@pytest.mark.asyncio
async def test_verify_commerce_order_matches_paypal_items_and_capture(test_settings: Settings) -> None:
    raw = FakeMCPRawSession({"get_order": _paypal_commerce_order()})
    assistant = PayPalAssistant(FakeTextGenerator(), _client(test_settings, raw), _Deposits([]))
    result = await assistant.verify_order(_commerce_order())
    assert result.consistent, result.checks
    assert {c.label for c in result.checks} == {"PayPal order", "Order reference (custom_id)", "Amount", "Items", "Capture"}
    wrong = await PayPalAssistant(
        FakeTextGenerator(), _client(test_settings, FakeMCPRawSession({"get_order": _paypal_commerce_order(items=[{"sku": "mug-sentinel", "quantity": "5"}])})), _Deposits([])
    ).verify_order(_commerce_order())
    assert not wrong.consistent and next(c for c in wrong.checks if c.label == "Items").status == "mismatch"


@pytest.mark.asyncio
async def test_verify_commerce_order_refuses_mock_payments(test_settings: Settings) -> None:
    from server.schemas import PaymentMode

    assistant = PayPalAssistant(FakeTextGenerator(), _client(test_settings, FakeMCPRawSession({})), _Deposits([]))
    with pytest.raises(PayPalAssistantError):
        await assistant.verify_order(_commerce_order(payment_mode=PaymentMode.MOCK, simulated=True))
