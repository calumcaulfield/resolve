"""The commerce systems the agent acts on.

`CommerceBackend` is the seam between the agent and a real shop. The bundled
`InMemoryCommerceBackend` is a faithful simulator: it enforces the same rules a
real platform does (you cannot cancel a shipped order; you cannot refund more
than was paid; addresses are frozen once a parcel is with the carrier), so the
agent is exercised against realistic refusals rather than a backend that says
yes to everything.

A Magento / Shopify / BigCommerce implementation is a matter of satisfying this
protocol. Nothing above this module knows which one is in use.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from resolve.domain.enums import OrderStatus
from resolve.domain.schemas import Order


class CommerceError(Exception):
    """Base class for backend refusals. Carries a reason the agent can relay."""


class OrderNotFoundError(CommerceError):
    pass


class OperationNotPermittedError(CommerceError):
    pass


class PaymentRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_ref: str
    amount_gbp: float
    status: str  # succeeded | failed | refunded | pending
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    method: str = "card"
    last4: str = "0000"


class PaymentLink(BaseModel):
    url: str
    order_ref: str
    amount_gbp: float
    expires_at: datetime


@runtime_checkable
class CommerceBackend(Protocol):
    async def get_order(self, reference: str) -> Order: ...
    async def list_payments(self, reference: str) -> list[PaymentRecord]: ...
    async def create_payment_link(self, reference: str, amount_gbp: float) -> PaymentLink: ...
    async def issue_refund(
        self, reference: str, amount_gbp: float, reason: str
    ) -> PaymentRecord: ...
    async def cancel_order(self, reference: str) -> Order: ...
    async def update_shipping_address(self, reference: str, address: str) -> Order: ...


class InMemoryCommerceBackend:
    """Deterministic simulator used by tests, evals and the demo."""

    #: Terminal states in which a cancellation is refused.
    UNCANCELLABLE: ClassVar[frozenset[OrderStatus]] = frozenset(
        {OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.REFUNDED}
    )
    #: States in which the shipping address can no longer be changed.
    ADDRESS_LOCKED: ClassVar[frozenset[OrderStatus]] = frozenset(
        {OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.CANCELLED}
    )

    def __init__(
        self,
        orders: list[Order] | None = None,
        payments: list[PaymentRecord] | None = None,
    ) -> None:
        self._orders: dict[str, Order] = {o.reference.upper(): o for o in (orders or [])}
        self._payments: list[PaymentRecord] = list(payments or [])
        self.calls: list[tuple[str, dict[str, object]]] = []

    # -- seeding ----------------------------------------------------------

    def add_order(self, order: Order) -> None:
        self._orders[order.reference.upper()] = order

    def add_payment(self, payment: PaymentRecord) -> None:
        self._payments.append(payment)

    @property
    def orders(self) -> list[Order]:
        return list(self._orders.values())

    # -- protocol ---------------------------------------------------------

    async def get_order(self, reference: str) -> Order:
        self.calls.append(("get_order", {"reference": reference}))
        order = self._orders.get(reference.strip().upper())
        if order is None:
            raise OrderNotFoundError(f"No order found with reference {reference}")
        return order

    async def list_payments(self, reference: str) -> list[PaymentRecord]:
        self.calls.append(("list_payments", {"reference": reference}))
        ref = reference.strip().upper()
        if ref not in self._orders:
            raise OrderNotFoundError(f"No order found with reference {reference}")
        return [p for p in self._payments if p.order_ref.upper() == ref]

    async def create_payment_link(self, reference: str, amount_gbp: float) -> PaymentLink:
        self.calls.append(
            ("create_payment_link", {"reference": reference, "amount_gbp": amount_gbp})
        )
        order = await self.get_order(reference)
        if order.status not in {OrderStatus.PENDING_PAYMENT, OrderStatus.PROCESSING}:
            raise OperationNotPermittedError(
                f"Order {order.reference} is {order.status.value}; "
                "a payment link is not applicable."
            )
        if amount_gbp <= 0:
            raise OperationNotPermittedError("Payment link amount must be positive.")
        token = uuid.uuid5(uuid.NAMESPACE_URL, f"{order.reference}:{amount_gbp}").hex[:16]
        return PaymentLink(
            url=f"https://pay.example.com/l/{token}",
            order_ref=order.reference,
            amount_gbp=round(amount_gbp, 2),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

    async def issue_refund(self, reference: str, amount_gbp: float, reason: str) -> PaymentRecord:
        self.calls.append(
            ("issue_refund", {"reference": reference, "amount_gbp": amount_gbp, "reason": reason})
        )
        order = await self.get_order(reference)
        if order.status is OrderStatus.PENDING_PAYMENT:
            raise OperationNotPermittedError(
                f"Order {order.reference} has not been paid; there is nothing to refund."
            )
        refundable = round(order.total_gbp - order.refunded_gbp, 2)
        if amount_gbp <= 0:
            raise OperationNotPermittedError("Refund amount must be positive.")
        if amount_gbp > refundable + 0.001:
            raise OperationNotPermittedError(
                f"Refund of £{amount_gbp:.2f} exceeds the refundable balance "
                f"of £{refundable:.2f} on order {order.reference}."
            )
        order.refunded_gbp = round(order.refunded_gbp + amount_gbp, 2)
        if abs(order.refunded_gbp - order.total_gbp) < 0.005:
            order.status = OrderStatus.REFUNDED
        record = PaymentRecord(
            order_ref=order.reference,
            amount_gbp=round(-amount_gbp, 2),
            status="refunded",
            method="refund",
        )
        self._payments.append(record)
        return record

    async def cancel_order(self, reference: str) -> Order:
        self.calls.append(("cancel_order", {"reference": reference}))
        order = await self.get_order(reference)
        if order.status in self.UNCANCELLABLE:
            raise OperationNotPermittedError(
                f"Order {order.reference} is {order.status.value} and can no longer be cancelled."
            )
        order.status = OrderStatus.CANCELLED
        return order

    async def update_shipping_address(self, reference: str, address: str) -> Order:
        self.calls.append(("update_shipping_address", {"reference": reference, "address": address}))
        order = await self.get_order(reference)
        if order.status in self.ADDRESS_LOCKED:
            raise OperationNotPermittedError(
                f"Order {order.reference} is {order.status.value}; the delivery address is locked."
            )
        if not address.strip():
            raise OperationNotPermittedError("A replacement address is required.")
        order.shipping_address = address.strip()
        return order
