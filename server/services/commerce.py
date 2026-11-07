"""Agent commerce: pay-per-access items, PayPal subscriptions, and product purchases.

Next to the refundable crawl deposit, an SARA site sells three more kinds of offer
to AI agents, all paid through PayPal:

- access: a fixed, non-refundable charge that unlocks one resource (video, dataset, audio)
- subscription: a recurring PayPal subscription that unlocks every page under a prefix
- product: an itemized order for digital (download) or physical (shipped) goods

The site, never the agent, sets every price: checkout requests name offers by ID and the
server prices them from the catalog. Payment is confirmed with PayPal before anything is
granted. A paid order or an active subscription yields one bearer token, returned once
and stored only as a SHA-256 hash, that unlocks exactly what was bought at /paid/{path}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Union

from server.core.security import (
    atomic_write_json,
    generate_bearer_token,
    hash_bearer_token,
    safe_load_json,
    utc_now_iso,
)
from server.schemas import (
    AccessItem,
    CommerceCatalogFile,
    CommerceOrderRecord,
    CommerceOrderResponse,
    OrderLine,
    Product,
    ProtectedPage,
    SubscriptionPlan,
    SubscriptionRecord,
    SubscriptionResponse,
)
from server.services.audit_logger import AuditLogger
from server.services.payment_methods import record_payment_method, resolve_payment_token
from server.services.payment_service import (
    OrderLineItem,
    PaymentApprovalRequiredError,
    PaymentProvider,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)

logger = logging.getLogger("sara.commerce")

CENT = Decimal("0.01")
DB_KEYS = ("orders", "subscriptions", "tokens", "plans")
COMMERCE_CALLBACK_PATH = "/commerce/paypal"


class CommerceError(ValueError):
    """A commerce request the server refuses; carries an HTTP status and error code."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "commerce_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _money(value: Union[str, Decimal]) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


class CommerceCatalog:
    """The site's offer catalog, validated at startup."""

    def __init__(self, path: Path, currency: str) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.file = CommerceCatalogFile.model_validate(raw)
        self.currency = currency
        self.access_items: dict[str, AccessItem] = {}
        self.access_by_path: dict[str, AccessItem] = {}
        for item in self.file.access_items:
            if item.item_id in self.access_items or item.path in self.access_by_path:
                raise ValueError(f"Duplicate access item {item.item_id} or path {item.path}")
            if _money(item.price) <= 0:
                raise ValueError(f"Access item {item.item_id} needs a positive price")
            self.access_items[item.item_id] = item
            self.access_by_path[item.path] = item
        self.plans: dict[str, SubscriptionPlan] = {}
        for plan in self.file.subscription_plans:
            if plan.plan_id in self.plans or _money(plan.price) <= 0:
                raise ValueError(f"Invalid or duplicate subscription plan {plan.plan_id}")
            self.plans[plan.plan_id] = plan
        for page_path in self.file.members_pages:
            if not any(page_path == p.path_prefix or page_path.startswith(p.path_prefix + "/") for p in self.plans.values()):
                raise ValueError(f"Members page {page_path} is not covered by any subscription plan")
        self.products: dict[str, Product] = {}
        for product in self.file.products:
            if product.sku in self.products or _money(product.price) <= 0:
                raise ValueError(f"Invalid or duplicate product {product.sku}")
            if product.fulfillment == "digital" and product.download_path not in self.file.downloads:
                raise ValueError(f"Digital product {product.sku} needs a download page")
            self.products[product.sku] = product

    @property
    def merchant_name(self) -> str:
        return self.file.merchant_name

    def page_for(self, path: str) -> Optional[ProtectedPage]:
        item = self.access_by_path.get(path)
        if item is not None:
            return ProtectedPage(title=item.title, content_type=item.content_type, body=item.body)
        return self.file.members_pages.get(path) or self.file.downloads.get(path)

    def plans_covering(self, path: str) -> list[SubscriptionPlan]:
        return [p for p in self.plans.values() if path == p.path_prefix or path.startswith(p.path_prefix + "/")]

    def products_unlocking(self, path: str) -> list[Product]:
        return [p for p in self.products.values() if p.download_path == path]

    def public_offers(self) -> dict[str, Any]:
        """The catalog as agents see it: no protected bodies."""
        currency = self.currency
        return {
            "pay_per_access": [
                {
                    "item_id": i.item_id,
                    "kind": i.kind,
                    "title": i.title,
                    "description": i.description,
                    "price": f"{_money(i.price):.2f}",
                    "currency": currency,
                    "refundable": False,
                    "unlocks": i.path,
                }
                for i in self.access_items.values()
            ],
            "subscriptions": [
                {
                    "plan_id": p.plan_id,
                    "name": p.name,
                    "description": p.description,
                    "price": f"{_money(p.price):.2f}",
                    "currency": currency,
                    "billing": f"every {p.interval_count} {p.interval_unit.lower()}{'s' if p.interval_count > 1 else ''}",
                    "unlocks_prefix": p.path_prefix,
                    "pages": sorted(path for path in self.file.members_pages if path.startswith(p.path_prefix)),
                    "benefits": p.benefits,
                    "requires_payer_approval": True,
                }
                for p in self.plans.values()
            ],
            "products": [
                {
                    "sku": p.sku,
                    "name": p.name,
                    "description": p.description,
                    "price": f"{_money(p.price):.2f}",
                    "shipping": f"{_money(p.shipping):.2f}",
                    "currency": currency,
                    "fulfillment": p.fulfillment,
                    "download": p.download_path,
                }
                for p in self.products.values()
            ],
        }


@dataclass(frozen=True)
class AccessGrant:
    kind: str  # order | subscription
    ref: str
    agent_id: str


class CommerceManager:
    """Creates and settles commerce orders and subscriptions; issues and checks access tokens."""

    def __init__(
        self,
        *,
        db_path: Path,
        catalog: CommerceCatalog,
        payment_provider: PaymentProvider,
        audit_logger: AuditLogger,
    ) -> None:
        self._db_path = db_path
        self.catalog = catalog
        self._provider = payment_provider
        self._audit = audit_logger
        self._lock = asyncio.Lock()
        self._orders: dict[str, CommerceOrderRecord] = {}
        self._subscriptions: dict[str, SubscriptionRecord] = {}
        self._tokens: dict[str, dict[str, str]] = {}  # token hash -> {kind, ref}
        self._plans: dict[str, str] = {}  # plan key -> provider plan id
        self._webhooks: dict[str, dict[str, str]] = {}  # processed PayPal webhook event ids
        # Set by the app: saved payment methods (PaymentMethodStore).
        self.payment_methods: Any = None

    @property
    def payment_provider(self) -> PaymentProvider:
        return self._provider

    def set_payment_provider(self, provider: PaymentProvider) -> None:
        self._provider = provider

    # --- persistence ------------------------------------------------------------------

    def load_from_disk(self) -> None:
        data = safe_load_json(self._db_path, DB_KEYS) if self._db_path.exists() else {k: {} for k in DB_KEYS}
        self._orders = {k: CommerceOrderRecord.model_validate(v) for k, v in data["orders"].items()}
        self._subscriptions = {k: SubscriptionRecord.model_validate(v) for k, v in data["subscriptions"].items()}
        self._tokens = {k: dict(v) for k, v in data["tokens"].items()}
        self._plans = {k: str(v) for k, v in data["plans"].items()}
        webhooks = data.get("webhooks")  # added later; older files have no such key
        self._webhooks = {k: dict(v) for k, v in webhooks.items()} if isinstance(webhooks, dict) else {}

    def _persist(self) -> None:
        atomic_write_json(
            self._db_path,
            {
                "orders": {k: v.model_dump(mode="json") for k, v in self._orders.items()},
                "subscriptions": {k: v.model_dump(mode="json") for k, v in self._subscriptions.items()},
                "tokens": self._tokens,
                "plans": self._plans,
                "webhooks": self._webhooks,
            },
        )

    def _issue_token(self, kind: str, ref: str) -> tuple[str, str]:
        raw = generate_bearer_token()
        token_hash = hash_bearer_token(raw)
        self._tokens[token_hash] = {"kind": kind, "ref": ref}
        return raw, token_hash

    # --- views ------------------------------------------------------------------------

    @staticmethod
    def order_response(record: CommerceOrderRecord, access_token: Optional[str] = None) -> CommerceOrderResponse:
        data = record.model_dump(exclude={"access_token_hash", "purpose", "dispute_id", "agent_pays", "save_payment_method"})
        return CommerceOrderResponse(**data, access_token=access_token)

    @staticmethod
    def subscription_response(record: SubscriptionRecord, access_token: Optional[str] = None) -> SubscriptionResponse:
        data = record.model_dump(exclude={"access_token_hash", "purpose", "provider_plan_id"})
        return SubscriptionResponse(**data, access_token=access_token)

    async def list_orders(self) -> list[CommerceOrderRecord]:
        async with self._lock:
            return sorted(self._orders.values(), key=lambda r: r.created_at, reverse=True)

    async def list_subscriptions(self) -> list[SubscriptionRecord]:
        async with self._lock:
            return sorted(self._subscriptions.values(), key=lambda r: r.created_at, reverse=True)

    def _own_order(self, order_id: str, agent_id: str) -> CommerceOrderRecord:
        record = self._orders.get(order_id)
        if record is None or record.agent_id != agent_id:
            raise CommerceError(f"Unknown order {order_id}.", status_code=404, code="order_not_found")
        return record

    def _own_subscription(self, ref: str, agent_id: str) -> SubscriptionRecord:
        record = self._subscriptions.get(ref)
        if record is None or record.agent_id != agent_id:
            raise CommerceError(f"Unknown subscription {ref}.", status_code=404, code="subscription_not_found")
        return record

    async def get_order(self, order_id: str, agent_id: str) -> CommerceOrderRecord:
        async with self._lock:
            return self._own_order(order_id, agent_id)

    async def get_subscription(self, ref: str, agent_id: str) -> SubscriptionRecord:
        async with self._lock:
            return self._own_subscription(ref, agent_id)

    # --- one-off orders: pay-per-access and products --------------------------------

    def _price_order(
        self, offer_type: str, item_id: Optional[str], items: list[tuple[str, int]]
    ) -> tuple[list[OrderLine], Decimal, list[str], bool]:
        """Lines, shipping, grants and whether anything ships, all from the catalog."""
        if offer_type == "access":
            if not item_id or items:
                raise CommerceError("A pay-per-access checkout names exactly one item_id.", code="invalid_checkout")
            item = self.catalog.access_items.get(item_id)
            if item is None:
                raise CommerceError(f"Unknown access item {item_id}.", status_code=404, code="unknown_offer")
            line = OrderLine(ref=item.item_id, name=item.title, unit_price=f"{_money(item.price):.2f}", quantity=1, category="DIGITAL_GOODS")
            return [line], Decimal("0.00"), [item.path], False

        if item_id or not items:
            raise CommerceError("A product checkout lists items as [{sku, quantity}].", code="invalid_checkout")
        lines: list[OrderLine] = []
        grants: list[str] = []
        shipping = Decimal("0.00")
        seen: set[str] = set()
        for sku, quantity in items:
            if sku in seen:
                raise CommerceError(f"SKU {sku} is listed twice.", code="invalid_checkout")
            seen.add(sku)
            product = self.catalog.products.get(sku)
            if product is None:
                raise CommerceError(f"Unknown product {sku}.", status_code=404, code="unknown_offer")
            physical = product.fulfillment == "physical"
            lines.append(
                OrderLine(
                    ref=product.sku,
                    name=product.name,
                    unit_price=f"{_money(product.price):.2f}",
                    quantity=quantity,
                    category="PHYSICAL_GOODS" if physical else "DIGITAL_GOODS",
                )
            )
            # One shipping charge per physical line (flat rate per shipped item type).
            shipping += _money(product.shipping) if physical else Decimal("0.00")
            if product.download_path:
                grants.append(product.download_path)
        return lines, shipping, grants, any(line.category == "PHYSICAL_GOODS" for line in lines)

    async def create_order(
        self,
        *,
        agent_id: str,
        offer_type: str,
        item_id: Optional[str],
        items: list[tuple[str, int]],
        purpose: Optional[str],
        buyer: str = "agent",
        save_payment_method: bool = False,
        payment_method_id: Optional[str] = None,
    ) -> CommerceOrderRecord:
        lines, shipping, grants, _ships = self._price_order(offer_type, item_id, items)
        currency = self.catalog.currency
        # An agent's order is created with no payment method attached, so nothing is charged
        # while the agent's spending mandate and purchase review decide; the saved method (or
        # the Sandbox test card) is attached at capture.
        agent_pays = buyer == "agent" and (bool(payment_method_id) or self._provider.autopay)
        if payment_method_id:
            if self.payment_methods is None or self.payment_methods.label(payment_method_id) is None:
                raise CommerceError("Unknown payment_method_id", status_code=404, code="payment_method_not_found")
        line_items = [
            OrderLineItem(name=l.name, unit_amount=_money(l.unit_price), quantity=l.quantity, sku=l.ref, category=l.category)
            for l in lines
        ]
        total = sum((_money(l.unit_price) * l.quantity for l in lines), Decimal("0.00")) + shipping
        order_id = f"ord_{secrets.token_hex(8)}"
        description = (
            f"{self.catalog.merchant_name}: {lines[0].name}"
            if len(lines) == 1
            else f"{self.catalog.merchant_name}: {len(lines)} items"
        )
        try:
            created = await self._provider.create_order(
                deposit_id=order_id,
                amount=total,
                currency=currency,
                request_id=f"{order_id}-create",
                description=description,
                intent="CAPTURE",
                items=line_items,
                shipping=shipping,
                callback_path=COMMERCE_CALLBACK_PATH,
                autopay=False,
                defer_payment=agent_pays,
            )
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal order creation failed: {exc}", status_code=502, code="payment_provider_error") from exc

        record = CommerceOrderRecord(
            order_id=order_id,
            kind=offer_type,  # type: ignore[arg-type]
            agent_id=agent_id,
            status="CREATED",
            amount=f"{total:.2f}",
            currency=currency,
            lines=lines,
            shipping=f"{shipping:.2f}",
            purpose=purpose,
            payment_mode=self._provider.mode,
            simulated=created.simulated,
            provider_order_id=created.order_id,
            approval_url=created.approval_url if not created.simulated and not agent_pays else None,
            fulfillment_status="PENDING_PAYMENT",
            grants=grants,
            created_at=utc_now_iso(),
        )
        record.agent_pays = agent_pays
        record.save_payment_method = bool(save_payment_method and agent_pays and not payment_method_id)
        if payment_method_id:
            record.payment_method_id = payment_method_id
            record.payment_method_label = self.payment_methods.label(payment_method_id)
        async with self._lock:
            self._orders[order_id] = record
            self._persist()
        await self._audit.log_event(
            event_type="COMMERCE_ORDER_CREATED",
            crawler_id=agent_id,
            payment_mode=self._provider.mode.value,
            details={
                "order_id": order_id,
                "offer_type": offer_type,
                "amount": record.amount,
                "currency": currency,
                "refs": [l.ref for l in lines],
                "provider_order_id": created.order_id,
            },
        )
        return record

    async def capture_order(self, *, order_id: str, agent_id: str) -> tuple[CommerceOrderRecord, Optional[str]]:
        """Capture and verify with PayPal; on first success issue the access token."""
        async with self._lock:
            record = self._own_order(order_id, agent_id)
            if record.status == "PAID":
                # A webhook may confirm the payment first; the token is then issued here.
                if record.grants and record.access_token_hash is None:
                    raw, token_hash = self._issue_token("order", order_id)
                    record = record.model_copy(update={"access_token_hash": token_hash})
                    self._orders[order_id] = record
                    self._persist()
                    return record, raw
                return record, None
            if record.status in ("CANCELLED", "REFUNDED"):
                raise CommerceError(f"Order {order_id} is {record.status.lower()}.", status_code=409, code="order_closed")
        try:
            if record.agent_pays:
                # Now that the agent has decided to pay, attach its payment method.
                token = resolve_payment_token(self.payment_methods, record.payment_method_id)
                await self._provider.confirm_payment_source(
                    order_id=str(record.provider_order_id),
                    request_id=f"{order_id}-confirm",
                    payment_token=token,
                    save_method=record.save_payment_method,
                )
            verified = await self._provider.capture_order(
                order_id=str(record.provider_order_id),
                request_id=f"{order_id}-capture",
                expected_amount=_money(record.amount),
                expected_currency=record.currency,
            )
        except PaymentApprovalRequiredError as exc:
            async with self._lock:
                record = self._orders[order_id].model_copy(
                    update={"status": "APPROVAL_REQUIRED", "approval_url": exc.approval_url or record.approval_url}
                )
                self._orders[order_id] = record
                self._persist()
            return record, None
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal capture failed: {exc}", status_code=502, code="payment_provider_error") from exc

        raw_token: Optional[str] = None
        async with self._lock:
            current = self._orders[order_id]
            if current.status == "PAID":
                return current, None
            if not verified.verified:
                current = current.model_copy(update={"status": "FAILED", "failure_reason": verified.failure_reason})
            else:
                ships = any(l.category == "PHYSICAL_GOODS" for l in current.lines)
                token_hash = None
                if current.grants:
                    raw_token, token_hash = self._issue_token("order", order_id)
                current = current.model_copy(
                    update={
                        "status": "PAID",
                        **dict(zip(("payment_method_id", "payment_method_label"), record_payment_method(
                            self.payment_methods, current.payment_method_id, verified.saved_method, agent_id, f"order {order_id}"
                        ))),
                        "provider_capture_id": verified.capture_id,
                        "paid_at": utc_now_iso(),
                        "approval_url": None,
                        "fulfillment_status": "PROCESSING" if ships else "DELIVERED",
                        "access_token_hash": token_hash,
                    }
                )
            self._orders[order_id] = current
            self._persist()
        await self._audit.log_event(
            event_type="COMMERCE_ORDER_PAID" if current.status == "PAID" else "COMMERCE_ORDER_FAILED",
            crawler_id=agent_id,
            allowed=current.status == "PAID",
            payment_mode=current.payment_mode.value,
            details={
                "order_id": order_id,
                "offer_type": current.kind,
                "amount": current.amount,
                "currency": current.currency,
                "provider_capture_id": current.provider_capture_id,
                "failure_reason": current.failure_reason,
            },
        )
        return current, raw_token

    async def cancel_order(self, *, order_id: str, agent_id: str) -> CommerceOrderRecord:
        """Abandon an unpaid order (for example when the person declines the purchase)."""
        async with self._lock:
            record = self._own_order(order_id, agent_id)
            if record.status == "PAID":
                raise CommerceError("A paid order cannot be cancelled here.", status_code=409, code="order_already_paid")
            if record.status == "CANCELLED":
                return record
            record = record.model_copy(update={"status": "CANCELLED", "approval_url": None, "fulfillment_status": None})
            self._orders[order_id] = record
            self._persist()
        await self._audit.log_event(
            event_type="COMMERCE_ORDER_CANCELLED",
            crawler_id=agent_id,
            payment_mode=record.payment_mode.value,
            details={"order_id": order_id, "amount": record.amount, "currency": record.currency},
        )
        return record

    # --- subscriptions ----------------------------------------------------------------

    async def _provider_plan_id(self, plan: SubscriptionPlan) -> str:
        key = f"{self._provider.mode.value}:{plan.plan_id}:{_money(plan.price)}:{self.catalog.currency}:{plan.interval_unit}:{plan.interval_count}"
        async with self._lock:
            known = self._plans.get(key)
        if known:
            return known
        plan_id = await self._provider.create_subscription_plan(
            name=f"{self.catalog.merchant_name} {plan.name}",
            description=plan.description,
            price=_money(plan.price),
            currency=self.catalog.currency,
            interval_unit=plan.interval_unit,
            interval_count=plan.interval_count,
            request_id=f"plan-{secrets.token_hex(6)}",
        )
        async with self._lock:
            self._plans[key] = plan_id
            self._persist()
        return plan_id

    async def subscribe(self, *, agent_id: str, plan_id: str, purpose: Optional[str]) -> tuple[SubscriptionRecord, Optional[str]]:
        plan = self.catalog.plans.get(plan_id)
        if plan is None:
            raise CommerceError(f"Unknown subscription plan {plan_id}.", status_code=404, code="unknown_offer")
        async with self._lock:
            for existing in self._subscriptions.values():
                if existing.agent_id == agent_id and existing.plan_id == plan_id and existing.status in ("ACTIVE", "APPROVAL_PENDING", "APPROVED"):
                    raise CommerceError(
                        f"This agent already has a {existing.status.lower()} {plan.name} subscription ({existing.subscription_ref}).",
                        status_code=409,
                        code="already_subscribed",
                    )
        ref = f"sub_{secrets.token_hex(8)}"
        try:
            provider_plan = await self._provider_plan_id(plan)
            result = await self._provider.create_subscription(plan_id=provider_plan, custom_id=ref, request_id=f"{ref}-create")
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal subscription failed: {exc}", status_code=502, code="payment_provider_error") from exc

        raw_token: Optional[str] = None
        token_hash: Optional[str] = None
        status = result.status if result.status in ("APPROVAL_PENDING", "APPROVED", "ACTIVE") else "FAILED"
        async with self._lock:
            if status == "ACTIVE":
                raw_token, token_hash = self._issue_token("subscription", ref)
            record = SubscriptionRecord(
                subscription_ref=ref,
                plan_id=plan.plan_id,
                plan_name=plan.name,
                agent_id=agent_id,
                status=status,  # type: ignore[arg-type]
                amount=f"{_money(plan.price):.2f}",
                currency=self.catalog.currency,
                interval_unit=plan.interval_unit,
                interval_count=plan.interval_count,
                path_prefix=plan.path_prefix,
                purpose=purpose,
                payment_mode=self._provider.mode,
                simulated=result.simulated,
                provider_plan_id=provider_plan,
                provider_subscription_id=result.subscription_id,
                approval_url=result.approval_url if status != "ACTIVE" else None,
                next_billing_time=result.next_billing_time,
                access_token_hash=token_hash,
                created_at=utc_now_iso(),
                activated_at=utc_now_iso() if status == "ACTIVE" else None,
                failure_reason=None if status != "FAILED" else f"PayPal returned status {result.status}",
            )
            self._subscriptions[ref] = record
            self._persist()
        await self._audit.log_event(
            event_type="SUBSCRIPTION_CREATED",
            crawler_id=agent_id,
            payment_mode=record.payment_mode.value,
            details={
                "subscription_ref": ref,
                "plan_id": plan.plan_id,
                "amount": record.amount,
                "currency": record.currency,
                "status": record.status,
                "provider_subscription_id": record.provider_subscription_id,
            },
        )
        return record, raw_token

    async def activate_subscription(self, *, ref: str, agent_id: str) -> tuple[SubscriptionRecord, Optional[str]]:
        """Confirm with PayPal that the subscriber approved; issue the token when ACTIVE."""
        async with self._lock:
            record = self._own_subscription(ref, agent_id)
            if record.status == "ACTIVE" and record.access_token_hash is None:
                raw, token_hash = self._issue_token("subscription", ref)
                record = record.model_copy(update={"access_token_hash": token_hash})
                self._subscriptions[ref] = record
                self._persist()
                return record, raw
        if record.status == "ACTIVE" or record.status in ("CANCELLED", "EXPIRED", "FAILED"):
            return record, None
        try:
            result = await self._provider.get_subscription(subscription_id=str(record.provider_subscription_id))
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal subscription lookup failed: {exc}", status_code=502, code="payment_provider_error") from exc
        raw_token: Optional[str] = None
        async with self._lock:
            current = self._subscriptions[ref]
            update: dict[str, Any] = {"next_billing_time": result.next_billing_time}
            if result.status == "ACTIVE" and current.status != "ACTIVE":
                raw_token, token_hash = self._issue_token("subscription", ref)
                update.update(status="ACTIVE", activated_at=utc_now_iso(), approval_url=None, access_token_hash=token_hash)
            elif result.status in ("APPROVAL_PENDING", "APPROVED", "SUSPENDED", "CANCELLED", "EXPIRED"):
                update["status"] = result.status
            current = current.model_copy(update=update)
            self._subscriptions[ref] = current
            self._persist()
        if raw_token:
            await self._audit.log_event(
                event_type="SUBSCRIPTION_ACTIVATED",
                crawler_id=agent_id,
                allowed=True,
                payment_mode=current.payment_mode.value,
                details={"subscription_ref": ref, "plan_id": current.plan_id, "amount": current.amount},
            )
        return current, raw_token

    async def cancel_subscription(self, *, ref: str, agent_id: str) -> SubscriptionRecord:
        async with self._lock:
            record = self._own_subscription(ref, agent_id)
        if record.status in ("CANCELLED", "EXPIRED", "FAILED"):
            return record
        try:
            if record.provider_subscription_id:
                await self._provider.cancel_subscription(
                    subscription_id=record.provider_subscription_id, reason="Cancelled by the subscribing agent"
                )
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal cancellation failed: {exc}", status_code=502, code="payment_provider_error") from exc
        async with self._lock:
            current = self._subscriptions[ref].model_copy(update={"status": "CANCELLED", "cancelled_at": utc_now_iso()})
            self._subscriptions[ref] = current
            self._persist()
        await self._audit.log_event(
            event_type="SUBSCRIPTION_CANCELLED",
            crawler_id=agent_id,
            payment_mode=current.payment_mode.value,
            details={"subscription_ref": ref, "plan_id": current.plan_id},
        )
        return current

    # --- publisher operations: refunds and shipment tracking -------------------------

    async def refund_order(self, *, order_id: str, reason: str) -> CommerceOrderRecord:
        """Refund a paid order through PayPal (publisher action). Access ends with it."""
        async with self._lock:
            record = self._orders.get(order_id)
            if record is None:
                raise CommerceError(f"Unknown order {order_id}.", status_code=404, code="order_not_found")
            if record.status == "REFUNDED":
                return record
            if record.status != "PAID" or not record.provider_capture_id:
                raise CommerceError("Only a paid order can be refunded.", status_code=409, code="order_not_paid")
        try:
            result = await self._provider.refund_capture(
                capture_id=record.provider_capture_id,
                order_id=record.provider_order_id,
                amount=_money(record.amount),
                currency=record.currency,
                request_id=f"{order_id}-refund",
                reason=reason[:120] or "Refunded by the merchant",
                reconcile_before_refund=True,
            )
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal refund failed: {exc}", status_code=502, code="payment_provider_error") from exc
        if result.status not in ("COMPLETED", "PENDING"):
            raise CommerceError(f"PayPal refund failed: {result.failure_reason or result.status}", status_code=502, code="refund_failed")
        record = await self._mark_refunded(order_id, result.refund_id)
        await self._audit.log_event(
            event_type="COMMERCE_ORDER_REFUNDED",
            crawler_id=record.agent_id,
            payment_mode=record.payment_mode.value,
            details={"order_id": order_id, "amount": record.amount, "provider_refund_id": result.refund_id, "refund_status": result.status},
        )
        return record

    async def _mark_refunded(self, order_id: str, refund_id: Optional[str]) -> CommerceOrderRecord:
        async with self._lock:
            record = self._orders[order_id].model_copy(
                update={"status": "REFUNDED", "provider_refund_id": refund_id, "refunded_at": utc_now_iso()}
            )
            self._orders[order_id] = record
            self._persist()
        return record

    async def add_tracking(self, *, order_id: str, tracking_number: str, carrier: str) -> CommerceOrderRecord:
        """Attach shipment tracking at PayPal and mark the order shipped (publisher action)."""
        async with self._lock:
            record = self._orders.get(order_id)
            if record is None:
                raise CommerceError(f"Unknown order {order_id}.", status_code=404, code="order_not_found")
            if record.status != "PAID" or not any(l.category == "PHYSICAL_GOODS" for l in record.lines):
                raise CommerceError("Only a paid order with shipped goods can be tracked.", status_code=409, code="not_shippable")
        try:
            await self._provider.add_tracking(
                order_id=str(record.provider_order_id),
                capture_id=str(record.provider_capture_id),
                tracking_number=tracking_number,
                carrier=carrier,
                request_id=f"{order_id}-track-{len(record.tracking) + 1}",
            )
        except PaymentProviderError as exc:
            raise CommerceError(f"PayPal tracking failed: {exc}", status_code=502, code="payment_provider_error") from exc
        async with self._lock:
            current = self._orders[order_id]
            record = current.model_copy(
                update={
                    "fulfillment_status": "SHIPPED",
                    "tracking": [*current.tracking, {"tracking_number": tracking_number, "carrier": carrier, "added_at": utc_now_iso()}],
                }
            )
            self._orders[order_id] = record
            self._persist()
        await self._audit.log_event(
            event_type="COMMERCE_ORDER_SHIPPED",
            crawler_id=record.agent_id,
            details={"order_id": order_id, "carrier": carrier, "tracking_number": tracking_number},
        )
        return record

    # --- PayPal webhooks ----------------------------------------------------------------

    async def webhook_seen(self, event_id: str) -> bool:
        async with self._lock:
            return event_id in self._webhooks

    async def remember_webhook(self, event_id: str, event_type: str, outcome: str) -> None:
        async with self._lock:
            self._webhooks[event_id] = {"type": event_type, "outcome": outcome, "at": utc_now_iso()}
            if len(self._webhooks) > 2000:  # keep the newest 2000 event ids
                for key in sorted(self._webhooks, key=lambda k: self._webhooks[k]["at"])[:-2000]:
                    self._webhooks.pop(key, None)
            self._persist()

    def has_order(self, order_id: str) -> bool:
        return order_id in self._orders

    def subscription_ref_for(self, custom_id: Optional[str], provider_id: Optional[str]) -> Optional[str]:
        if custom_id and custom_id in self._subscriptions:
            return custom_id
        for ref, sub in self._subscriptions.items():
            if provider_id and sub.provider_subscription_id == provider_id:
                return ref
        return None

    async def confirm_order_from_webhook(self, order_id: str) -> str:
        """PAYMENT.CAPTURE.COMPLETED: confirm with PayPal itself before marking the order paid."""
        async with self._lock:
            record = self._orders.get(order_id)
        if record is None:
            return "unknown_order"
        if record.status == "PAID":
            async with self._lock:
                self._orders[order_id] = self._orders[order_id].model_copy(update={"confirmed_by_webhook": True})
                self._persist()
            return "already_paid"
        if record.status in ("CANCELLED", "REFUNDED", "FAILED"):
            return f"ignored_{record.status.lower()}"
        verified = await self._provider.verify_order(
            order_id=str(record.provider_order_id), expected_amount=_money(record.amount), expected_currency=record.currency
        )
        if not verified.verified:
            return "not_verified"
        ships = any(l.category == "PHYSICAL_GOODS" for l in record.lines)
        async with self._lock:
            self._orders[order_id] = self._orders[order_id].model_copy(
                update={
                    "status": "PAID",
                    "provider_capture_id": verified.capture_id,
                    "paid_at": utc_now_iso(),
                    "approval_url": None,
                    "fulfillment_status": "PROCESSING" if ships else "DELIVERED",
                    "confirmed_by_webhook": True,
                }
            )
            self._persist()
        return "marked_paid"

    async def refund_from_webhook(self, order_id: str, refund_id: Optional[str]) -> str:
        async with self._lock:
            record = self._orders.get(order_id)
        if record is None:
            return "unknown_order"
        if record.status == "REFUNDED":
            return "already_refunded"
        if not refund_id:
            return "not_verified"
        # Never trust the event body: read the refund back from PayPal first.
        try:
            details = await self._provider.get_refund_details(refund_id=refund_id)
        except PaymentProviderTimeoutError:
            raise
        except PaymentProviderError:
            return "not_verified"
        if (details.status or "").upper() != "COMPLETED":
            return f"refund_{(details.status or 'unknown').lower()}"
        await self._mark_refunded(order_id, refund_id)
        return "marked_refunded"

    async def dispute_from_webhook(self, order_id: str, dispute_id: Optional[str], status: str) -> str:
        async with self._lock:
            record = self._orders.get(order_id)
            if record is None:
                return "unknown_order"
            self._orders[order_id] = record.model_copy(update={"dispute_id": dispute_id, "dispute_status": status})
            self._persist()
        return "dispute_recorded"

    async def sync_subscription_from_webhook(self, ref: str) -> str:
        """BILLING.SUBSCRIPTION.*: read the authoritative status from PayPal."""
        async with self._lock:
            record = self._subscriptions.get(ref)
        if record is None or not record.provider_subscription_id:
            return "unknown_subscription"
        result = await self._provider.get_subscription(subscription_id=record.provider_subscription_id)
        update: dict[str, Any] = {"next_billing_time": result.next_billing_time}
        if result.status == "ACTIVE" and record.status != "ACTIVE":
            update.update(status="ACTIVE", activated_at=utc_now_iso(), approval_url=None)  # token on the agent's next call
        elif result.status in ("CANCELLED", "SUSPENDED", "EXPIRED") and record.status != result.status:
            update["status"] = result.status
            if result.status == "CANCELLED":
                update["cancelled_at"] = utc_now_iso()
        async with self._lock:
            self._subscriptions[ref] = self._subscriptions[ref].model_copy(update=update)
            self._persist()
            status = self._subscriptions[ref].status
        return f"status_{status.lower()}"

    async def subscription_payment_from_webhook(self, ref: str) -> str:
        async with self._lock:
            record = self._subscriptions.get(ref)
            if record is None:
                return "unknown_subscription"
            self._subscriptions[ref] = record.model_copy(
                update={"payments_received": record.payments_received + 1, "last_payment_at": utc_now_iso()}
            )
            self._persist()
        return "payment_recorded"

    # --- paid content -----------------------------------------------------------------

    async def authorize(self, raw_token: Optional[str], path: str) -> tuple[bool, Optional[AccessGrant], str]:
        """Whether `raw_token` unlocks `path`; returns (allowed, grant, reason_code)."""
        if not raw_token:
            return False, None, "PAYMENT_REQUIRED"
        async with self._lock:
            entry = self._tokens.get(hash_bearer_token(raw_token))
            if entry is None:
                return False, None, "INVALID_TOKEN"
            if entry["kind"] == "order":
                order = self._orders.get(entry["ref"])
                if order is None or order.status != "PAID":
                    return False, None, "ORDER_NOT_PAID"
                grant = AccessGrant(kind="order", ref=order.order_id, agent_id=order.agent_id)
                return (path in order.grants), grant, ("ALLOWED" if path in order.grants else "NOT_COVERED")
            sub = self._subscriptions.get(entry["ref"])
            if sub is None or sub.status != "ACTIVE":
                return False, None, "SUBSCRIPTION_NOT_ACTIVE"
            grant = AccessGrant(kind="subscription", ref=sub.subscription_ref, agent_id=sub.agent_id)
            covered = path == sub.path_prefix or path.startswith(sub.path_prefix + "/")
            return covered, grant, ("ALLOWED" if covered else "NOT_COVERED")

    def offers_for_path(self, path: str) -> list[dict[str, Any]]:
        """The ways to pay for a protected path (shown in its 402 response)."""
        currency = self.catalog.currency
        offers: list[dict[str, Any]] = []
        item = self.catalog.access_by_path.get(path)
        if item is not None:
            offers.append(
                {"offer_type": "access", "item_id": item.item_id, "title": item.title, "price": f"{_money(item.price):.2f}", "currency": currency}
            )
        for plan in self.catalog.plans_covering(path):
            offers.append(
                {
                    "offer_type": "subscription",
                    "plan_id": plan.plan_id,
                    "title": plan.name,
                    "price": f"{_money(plan.price):.2f}",
                    "currency": currency,
                    "billing": f"every {plan.interval_count} {plan.interval_unit.lower()}",
                }
            )
        for product in self.catalog.products_unlocking(path):
            offers.append(
                {"offer_type": "product", "sku": product.sku, "title": product.name, "price": f"{_money(product.price):.2f}", "currency": currency}
            )
        return offers

    # --- publisher summary ------------------------------------------------------------

    async def summary(self) -> dict[str, Any]:
        orders = await self.list_orders()
        subs = await self.list_subscriptions()
        paid = [o for o in orders if o.status == "PAID"]
        access_revenue = sum((_money(o.amount) for o in paid if o.kind == "access"), Decimal("0.00"))
        product_revenue = sum((_money(o.amount) for o in paid if o.kind == "product"), Decimal("0.00"))
        active = [s for s in subs if s.status == "ACTIVE"]
        mrr = Decimal("0.00")
        for s in active:
            monthly = {"DAY": Decimal("30"), "WEEK": Decimal("52") / Decimal("12"), "MONTH": Decimal("1"), "YEAR": Decimal("1") / Decimal("12")}
            mrr += (_money(s.amount) * monthly.get(s.interval_unit, Decimal("1")) / Decimal(s.interval_count)).quantize(CENT)
        return {
            "currency": self.catalog.currency,
            "access_revenue": f"{access_revenue:.2f}",
            "product_revenue": f"{product_revenue:.2f}",
            "subscription_mrr": f"{mrr:.2f}",
            "paid_access_orders": sum(1 for o in paid if o.kind == "access"),
            "paid_product_orders": sum(1 for o in paid if o.kind == "product"),
            "orders_awaiting_payment": sum(1 for o in orders if o.status in ("CREATED", "APPROVAL_REQUIRED")),
            "active_subscriptions": len(active),
            "pending_subscriptions": sum(1 for s in subs if s.status in ("APPROVAL_PENDING", "APPROVED")),
            "cancelled_subscriptions": sum(1 for s in subs if s.status == "CANCELLED"),
            "refunded_orders": sum(1 for o in orders if o.status == "REFUNDED"),
            "open_disputes": sum(1 for o in orders if o.dispute_status and o.dispute_status != "RESOLVED"),
            "webhook_events": len(self._webhooks),
            "orders": [o.model_dump(mode="json", exclude={"access_token_hash"}) for o in orders[:200]],
            "subscriptions": [s.model_dump(mode="json", exclude={"access_token_hash"}) for s in subs[:200]],
        }
