"""Tools for the AgentTrust personal agent: one agent, many ways to pay a website.

The agent acts for one person and can:
- discover what a site sells (its machine-readable offer catalog)
- crawl protected pages with a refundable deposit (the crawler tools, reused)
- buy a pay-per-access item (video, dataset, audio)
- subscribe to a plan (PayPal Subscriptions)
- buy products (an itemized PayPal order)
- open paid content it has bought

Money is governed by a spending mandate that the person sets and the code enforces, not
the model: a total budget, the payment types allowed, an amount above which the person
must approve, and a rule that recurring charges always need approval. Access tokens are
held here and never shown to the model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from client.agent_tools import (
    TOOL_DEFINITIONS as CRAWLER_TOOL_DEFINITIONS,
    MAX_PAGE_TEXT_CHARS,
    GatewayToolbox,
    HttpClient,
    _safe_json,
    _tool,
    html_to_text,
)

PAYMENT_TYPES = ("deposit", "access", "subscription", "product")
MAX_ASSISTANT_TOOL_CALLS = 80
CENT = Decimal("0.01")

# A purchase awaiting the person's decision: returns True to approve.
DecisionHandler = Callable[[dict[str, Any]], bool]
# Opens a PayPal checkout/subscription approval link and waits for the payer.
CheckoutHandler = Callable[[str], None]


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


@dataclass
class SpendingMandate:
    """What the person allows the agent to spend. Enforced in code before every payment."""

    budget: Decimal
    approval_threshold: Decimal
    allowed_types: frozenset[str] = frozenset(PAYMENT_TYPES)
    recurring_needs_approval: bool = True
    committed: Decimal = Decimal("0.00")
    returned: Decimal = Decimal("0.00")
    ledger: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return (self.budget - self.committed + self.returned).quantize(CENT)

    def snapshot(self) -> dict[str, Any]:
        return {
            "budget": f"{self.budget:.2f}",
            "spent_net": f"{(self.committed - self.returned):.2f}",
            "remaining": f"{self.remaining:.2f}",
            "approval_threshold": f"{self.approval_threshold:.2f}",
            "allowed_types": sorted(self.allowed_types),
            "recurring_needs_approval": self.recurring_needs_approval,
            "purchases": [dict(entry) for entry in self.ledger],
        }


ITEMS_SCHEMA = {
    "type": "array",
    "description": "Products to buy: each entry names a sku from the catalog and a quantity (1-10).",
    "items": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "description": "Product SKU from discover_offers."},
            "quantity": {"type": "integer", "description": "How many (1-10)."},
        },
        "required": ["sku", "quantity"],
        "additionalProperties": False,
    },
}

REASON = {"type": "string", "description": "One sentence for your user: why this purchase serves their request."}

ASSISTANT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "discover_offers",
        "Read the website's machine-readable offer catalog (/.well-known/agent-commerce.json): "
        "refundable crawl deposits by tier, pay-per-access items, subscription plans, products, "
        "prices, and how payment works. Call this first.",
        {},
    ),
    _tool(
        "check_mandate",
        "Show your user's spending mandate: budget, net spend so far, remaining budget, the "
        "amount above which purchases need the user's approval, allowed payment types, and "
        "every purchase made so far.",
        {},
    ),
    *CRAWLER_TOOL_DEFINITIONS,
    _tool(
        "buy_access",
        "Buy one pay-per-access item (video, dataset, audio) through PayPal. A fixed, "
        "non-refundable charge set by the site. The mandate is checked first; purchases above "
        "the approval threshold are sent to your user, who may decline. On success the item "
        "can be read with open_paid_content.",
        {"item_id": {"type": "string", "description": "item_id from discover_offers."}, "reason": REASON},
    ),
    _tool(
        "subscribe",
        "Subscribe to a recurring plan through PayPal Subscriptions. Recurring charges always "
        "need your user's approval, and PayPal may ask the payer to approve the subscription. "
        "On success every page under the plan's prefix opens with open_paid_content.",
        {"plan_id": {"type": "string", "description": "plan_id from discover_offers."}, "reason": REASON},
    ),
    _tool(
        "cancel_subscription",
        "Cancel a subscription you started (no further billing).",
        {"subscription_ref": {"type": "string", "description": "subscription_ref returned by subscribe."}},
    ),
    _tool(
        "purchase_products",
        "Buy products (digital or physical) in one itemized PayPal order. The site prices the "
        "cart, including shipping; the mandate is checked against that total before any money "
        "moves, and above the approval threshold your user decides.",
        {"items": ITEMS_SCHEMA, "reason": REASON},
    ),
    _tool(
        "open_paid_content",
        "Open paid content you have bought: a pay-per-access item (/media/...), a members page "
        "(/members/...), or a download (/downloads/...). Returns the content wrapped in "
        "<untrusted_page_content> tags, or the offers that unlock it if you have not paid.",
        {"path": {"type": "string", "description": "Content path such as '/media/video/...' or '/members/daily-feed'."}},
    ),
]


@dataclass
class AssistantToolbox:
    """Executes the personal agent's tool calls against an AgentTrust site."""

    http: HttpClient
    agent_id: str
    mandate: SpendingMandate
    decision_handler: DecisionHandler = lambda request: False
    checkout_handler: CheckoutHandler = lambda url: None
    sleep: Callable[[float], None] = time.sleep
    catalog: Optional[dict[str, Any]] = None
    orders: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    _tokens: list[tuple[str, list[str], Optional[str], str]] = field(default_factory=list, repr=False)
    crawler: GatewayToolbox = field(init=False)
    _deposit_entry: Optional[dict[str, Any]] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.crawler = GatewayToolbox(
            http=self.http,
            crawler_id=self.agent_id,
            max_deposit=self.mandate.remaining,
            approval_handler=lambda url: self.checkout_handler(url),
            sleep=self.sleep,
        )

    # --- dispatch --------------------------------------------------------------------

    def execute(self, name: str, tool_input: Any) -> tuple[str, bool]:
        args = tool_input if isinstance(tool_input, dict) else {}
        own: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "discover_offers": self._discover_offers,
            "check_mandate": lambda _a: {"mandate": self.mandate.snapshot()},
            "pay_deposit": self._pay_deposit,
            "complete_session": self._complete_session,
            "buy_access": self._buy_access,
            "subscribe": self._subscribe,
            "cancel_subscription": self._cancel_subscription,
            "purchase_products": self._purchase_products,
            "open_paid_content": self._open_paid_content,
        }
        self.calls += 1
        if self.calls > MAX_ASSISTANT_TOOL_CALLS:
            return json.dumps({"error": f"Tool call limit ({MAX_ASSISTANT_TOOL_CALLS}) reached. Report to your user and stop."}), True
        if name in own:
            try:
                result = own[name](args)
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                result = {"error": f"Invalid input for {name}: {exc}"}
            return json.dumps(result, indent=2), "error" in result
        if name in {tool["name"] for tool in CRAWLER_TOOL_DEFINITIONS}:
            return self.crawler.execute(name, args)
        return json.dumps({"error": f"Unknown tool '{name}'."}), True

    # --- mandate ---------------------------------------------------------------------

    def _authorize_spend(
        self, *, payment_type: str, amount: Decimal, title: str, reason: str, recurring: bool = False
    ) -> Optional[str]:
        """None when the payment may proceed; otherwise why it may not."""
        mandate = self.mandate
        if payment_type not in mandate.allowed_types:
            return f"Your user's mandate does not allow {payment_type} payments."
        if amount > mandate.remaining:
            return (
                f"{amount:.2f} exceeds the remaining budget of {mandate.remaining:.2f}. "
                "Do not try again; tell your user."
            )
        needs_approval = amount > mandate.approval_threshold or (recurring and mandate.recurring_needs_approval)
        if not needs_approval:
            return None
        currency = (self.catalog or {}).get("currency", "USD")
        approved = self.decision_handler(
            {
                "payment_type": payment_type,
                "title": title,
                "amount": f"{amount:.2f}",
                "currency": currency,
                "recurring": recurring,
                "reason": (reason or "").strip()[:300],
                "remaining_budget": f"{mandate.remaining:.2f}",
            }
        )
        if not approved:
            mandate.ledger.append(
                {"type": payment_type, "title": title, "amount": f"{amount:.2f}", "status": "DECLINED_BY_USER"}
            )
            return "Your user declined this purchase. Respect the decision: do not retry it or work around it."
        return None

    def _record(self, payment_type: str, ref: str, title: str, amount: Decimal, status: str, **extra: Any) -> dict[str, Any]:
        entry = {"type": payment_type, "ref": ref, "title": title, "amount": f"{amount:.2f}", "status": status, **extra}
        self.mandate.ledger.append(entry)
        if status in ("PAID", "ACTIVE", "HELD"):
            self.mandate.committed += amount
        return entry

    # --- tools -----------------------------------------------------------------------

    def _discover_offers(self, _args: dict[str, Any]) -> dict[str, Any]:
        resp = self.http.get("/.well-known/agent-commerce.json")
        if resp.status_code != 200:
            return {"error": f"Could not read the offer catalog (HTTP {resp.status_code})."}
        self.catalog = resp.json()
        offers = self.catalog.get("offers") or {}
        return {
            "merchant": self.catalog.get("merchant"),
            "currency": self.catalog.get("currency"),
            "payments": self.catalog.get("payments"),
            "offers": offers,
            "rules": self.catalog.get("rules"),
        }

    def _ensure_catalog(self) -> dict[str, Any]:
        if self.catalog is None:
            self._discover_offers({})
        return self.catalog or {}

    def _pay_deposit(self, args: dict[str, Any]) -> dict[str, Any]:
        crawler = self.crawler
        if crawler.deposit is not None and crawler.deposit.get("payment_status") in ("CAPTURED", "AUTHORIZED"):
            return json.loads(crawler.execute("pay_deposit", args)[0])
        if crawler.challenge is None:
            return {"error": "No payment challenge yet. Request a page with fetch_page first to receive the 402 challenge."}
        deposit = crawler.challenge.get("deposit") or {}
        amount = _money(deposit.get("amount"))
        pricing = crawler.challenge.get("pricing") or {}
        title = f"Refundable crawl deposit ({pricing.get('tier_name') or 'base'} tier)"
        refusal = self._authorize_spend(payment_type="deposit", amount=amount, title=title, reason=str(args.get("reason") or "Crawl deposit, refunded if I follow the site's rules."))
        if refusal:
            return {"error": refusal}
        crawler.max_deposit = self.mandate.remaining
        content, is_error = crawler.execute("pay_deposit", args)
        result = json.loads(content)
        if not is_error and result.get("status") == "paid":
            self._deposit_entry = self._record("deposit", str(result.get("deposit_id")), title, amount, "HELD", refundable=True)
            result["mandate_remaining"] = f"{self.mandate.remaining:.2f}"
        return result

    def _complete_session(self, args: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(self.crawler.execute("complete_session", args)[0])
        entry = self._deposit_entry
        if entry is not None and result.get("deposit_state"):
            state = result["deposit_state"]
            entry["status"] = state
            if state in ("REFUNDED", "RELEASED"):
                self.mandate.returned += _money(entry["amount"])
            result["mandate_remaining"] = f"{self.mandate.remaining:.2f}"
        return result

    def _wait_for_payer(self, url: Optional[str]) -> None:
        if url:
            self.checkout_handler(url)

    def _buy_access(self, args: dict[str, Any]) -> dict[str, Any]:
        item_id = str(args["item_id"]).strip()
        return self._checkout(
            {"offer_type": "access", "item_id": item_id},
            payment_type="access",
            reason=str(args.get("reason") or ""),
        )

    def _purchase_products(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_items = args["items"]
        if not isinstance(raw_items, list) or not raw_items:
            return {"error": "items must be a non-empty list of {sku, quantity}."}
        items = [{"sku": str(i["sku"]).strip(), "quantity": int(i.get("quantity") or 1)} for i in raw_items]
        return self._checkout({"offer_type": "product", "items": items}, payment_type="product", reason=str(args.get("reason") or ""))

    def _checkout(self, body: dict[str, Any], *, payment_type: str, reason: str) -> dict[str, Any]:
        if payment_type not in self.mandate.allowed_types:
            return {"error": f"Your user's mandate does not allow {payment_type} payments."}
        # 1. The site prices the order. Creating it moves no money.
        created = self.http.post("/commerce/checkout", json={"agent_id": self.agent_id, "purpose": reason[:300] or None, **body})
        if created.status_code != 201:
            return {"error": f"Checkout failed (HTTP {created.status_code}).", "detail": _safe_json(created)}
        order = created.json()
        amount = _money(order["amount"])
        title = ", ".join(
            f"{line['quantity']}× {line['name']}" if int(line["quantity"]) > 1 else line["name"] for line in order["lines"]
        )[:160]
        if float(order.get("shipping") or 0) > 0:
            title += f" + shipping {order['shipping']}"
        # 2. The mandate is checked against the site's total before any money moves.
        refusal = self._authorize_spend(payment_type=payment_type, amount=amount, title=title, reason=reason)
        if refusal:
            # Nothing was paid; cancel the order so the site does not wait for payment.
            self.http.post(f"/commerce/orders/{order['order_id']}/cancel", json={"agent_id": self.agent_id})
            return {"error": refusal, "order_id": order["order_id"], "amount": order["amount"], "paid": False, "order_cancelled": True}
        # 3. Pay: approve at PayPal when needed, then capture and verify.
        self._wait_for_payer(order.get("approval_url"))
        captured = self.http.post(f"/commerce/orders/{order['order_id']}/capture", json={"agent_id": self.agent_id})
        if captured.status_code != 200:
            return {"error": f"Payment failed (HTTP {captured.status_code}).", "detail": _safe_json(captured)}
        paid = captured.json()
        if paid.get("status") == "APPROVAL_REQUIRED":
            self._wait_for_payer(paid.get("approval_url"))
            captured = self.http.post(f"/commerce/orders/{order['order_id']}/capture", json={"agent_id": self.agent_id})
            paid = captured.json() if captured.status_code == 200 else paid
        if paid.get("status") != "PAID":
            return {"error": f"The order was not paid (status {paid.get('status')}).", "failure_reason": paid.get("failure_reason")}
        token = paid.pop("access_token", None)
        if token:
            self._tokens.append((token, list(paid.get("grants") or []), None, paid["order_id"]))
        self._record(
            payment_type,
            paid["order_id"],
            title,
            amount,
            "PAID",
            paypal_order_id=paid.get("provider_order_id"),
            paypal_capture_id=paid.get("provider_capture_id"),
            fulfillment=paid.get("fulfillment_status"),
        )
        summary = {
            "status": "paid",
            "order_id": paid["order_id"],
            "amount": paid["amount"],
            "currency": paid["currency"],
            "lines": paid["lines"],
            "shipping": paid["shipping"],
            "fulfillment_status": paid.get("fulfillment_status"),
            "unlocks": paid.get("grants"),
            "paypal_order_id": paid.get("provider_order_id"),
            "paypal_capture_id": paid.get("provider_capture_id"),
            "simulated_payment": paid.get("simulated"),
            "mandate_remaining": f"{self.mandate.remaining:.2f}",
        }
        self.orders.append(summary)
        return summary

    def _subscribe(self, args: dict[str, Any]) -> dict[str, Any]:
        plan_id = str(args["plan_id"]).strip()
        reason = str(args.get("reason") or "")
        catalog = self._ensure_catalog()
        plans = {p.get("plan_id"): p for p in (catalog.get("offers") or {}).get("subscriptions") or []}
        plan = plans.get(plan_id)
        if plan is None:
            return {"error": f"Unknown plan {plan_id}. Use a plan_id from discover_offers."}
        amount = _money(plan["price"])
        title = f"{plan.get('name')} subscription ({plan.get('billing')})"
        refusal = self._authorize_spend(payment_type="subscription", amount=amount, title=title, reason=reason, recurring=True)
        if refusal:
            return {"error": refusal, "subscribed": False}
        created = self.http.post("/commerce/subscriptions", json={"agent_id": self.agent_id, "plan_id": plan_id, "purpose": reason[:300] or None})
        if created.status_code != 201:
            return {"error": f"Subscription failed (HTTP {created.status_code}).", "detail": _safe_json(created)}
        sub = created.json()
        if sub.get("status") != "ACTIVE":
            self._wait_for_payer(sub.get("approval_url"))
            activated = self.http.post(f"/commerce/subscriptions/{sub['subscription_ref']}/activate", json={"agent_id": self.agent_id})
            if activated.status_code == 200:
                sub = activated.json()
        if sub.get("status") != "ACTIVE":
            return {"error": f"The subscription is not active (status {sub.get('status')}). The payer may not have approved it at PayPal.", "subscription_ref": sub.get("subscription_ref")}
        token = sub.pop("access_token", None)
        if token:
            self._tokens.append((token, [], sub["path_prefix"], sub["subscription_ref"]))
        self._record(
            "subscription",
            sub["subscription_ref"],
            title,
            amount,
            "ACTIVE",
            recurring=True,
            paypal_subscription_id=sub.get("provider_subscription_id"),
        )
        summary = {
            "status": "subscribed",
            "subscription_ref": sub["subscription_ref"],
            "plan": sub["plan_name"],
            "amount": sub["amount"],
            "currency": sub["currency"],
            "billing": f"every {sub['interval_count']} {sub['interval_unit'].lower()}",
            "unlocks_prefix": sub["path_prefix"],
            "paypal_subscription_id": sub.get("provider_subscription_id"),
            "simulated_payment": sub.get("simulated"),
            "mandate_remaining": f"{self.mandate.remaining:.2f}",
        }
        self.subscriptions.append(summary)
        return summary

    def _cancel_subscription(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args["subscription_ref"]).strip()
        resp = self.http.post(f"/commerce/subscriptions/{ref}/cancel", json={"agent_id": self.agent_id})
        if resp.status_code != 200:
            return {"error": f"Cancellation failed (HTTP {resp.status_code}).", "detail": _safe_json(resp)}
        data = resp.json()
        for entry in self.mandate.ledger:
            if entry.get("ref") == ref:
                entry["status"] = "CANCELLED"
        return {"status": data.get("status"), "subscription_ref": ref}

    def _token_for(self, path: str) -> Optional[str]:
        for token, grants, prefix, _ref in self._tokens:
            if path in grants or (prefix and (path == prefix or path.startswith(prefix + "/"))):
                return token
        return None

    def _open_paid_content(self, args: dict[str, Any]) -> dict[str, Any]:
        path = str(args["path"]).strip()
        if not path.startswith("/") or ".." in path or len(path) > 200:
            return {"error": "path must start with '/' (for example /media/video/...)."}
        path = path[len("/paid"):] if path.startswith("/paid/") else path
        token = self._token_for(path)
        headers = {"Authorization": f"Bearer {token}"} if token else None
        resp = self.http.get(f"/paid{path}", headers=headers)
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            text = resp.text if "csv" in content_type or "plain" in content_type else html_to_text(resp.text)
            truncated = len(text) > MAX_PAGE_TEXT_CHARS
            return {
                "status": 200,
                "path": path,
                "content": (
                    "<untrusted_page_content>\n"
                    f"{text[:MAX_PAGE_TEXT_CHARS]}{' [truncated]' if truncated else ''}\n"
                    "</untrusted_page_content>"
                ),
            }
        body = _safe_json(resp)
        return {
            "status": resp.status_code,
            "path": path,
            "error": body.get("message") or "Not available.",
            "offers": body.get("offers"),
        }
