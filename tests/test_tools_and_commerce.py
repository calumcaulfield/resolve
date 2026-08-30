"""Tools and the commerce backend.

The backend refuses the things a real platform refuses. That matters: an agent
tested against a backend that always says yes has never been tested.
"""

from __future__ import annotations

import pytest

from resolve.agent.tools.commerce_tools import build_commerce_registry
from resolve.agent.tools.registry import ToolContext
from resolve.domain.enums import ActionRisk, OrderStatus
from resolve.services.commerce import (
    InMemoryCommerceBackend,
    OperationNotPermittedError,
    OrderNotFoundError,
)


@pytest.fixture
def backend(dataset) -> InMemoryCommerceBackend:
    return dataset.backend()


@pytest.fixture
def registry(backend):  # type: ignore[no-untyped-def]
    return build_commerce_registry(backend)


@pytest.fixture
def context(backend) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(commerce=backend, ticket_id="T1")


class TestRegistry:
    def test_risk_tiers_are_assigned_by_consequence(self, registry) -> None:
        assert registry.get("lookup_order").risk is ActionRisk.AUTO
        assert registry.get("issue_refund").risk is ActionRisk.REQUIRES_APPROVAL
        assert registry.get("cancel_order").risk is ActionRisk.REQUIRES_APPROVAL

    def test_catalogue_marks_approval_tools(self, registry) -> None:
        assert "[REQUIRES HUMAN APPROVAL]" in registry.catalogue()

    def test_duplicate_registration_is_rejected(self, registry) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            registry.register(registry.get("lookup_order"))

    async def test_unknown_tool_returns_an_error_not_an_exception(self, registry, context) -> None:
        result = await registry.execute("rm_rf", {}, context)
        assert not result.ok
        assert "unknown tool" in (result.error or "")

    async def test_invalid_arguments_are_rejected_before_the_handler(
        self, registry, context
    ) -> None:
        result = await registry.execute("issue_refund", {"order_ref": "ORD-400000"}, context)
        assert not result.ok
        assert "invalid arguments" in (result.error or "")

    async def test_extra_arguments_are_rejected(self, registry, context) -> None:
        result = await registry.execute(
            "lookup_order", {"order_ref": "ORD-400000", "sudo": True}, context
        )
        assert not result.ok


class TestCommerceRules:
    async def test_unknown_order_is_reported_clearly(self, backend) -> None:
        with pytest.raises(OrderNotFoundError):
            await backend.get_order("ORD-999999")

    async def test_cannot_cancel_a_shipped_order(self, backend, dataset) -> None:
        shipped = next(o for o in dataset.orders if o.status is OrderStatus.SHIPPED)
        with pytest.raises(OperationNotPermittedError, match="no longer be cancelled"):
            await backend.cancel_order(shipped.reference)

    async def test_cannot_change_the_address_after_dispatch(self, backend, dataset) -> None:
        shipped = next(o for o in dataset.orders if o.status is OrderStatus.SHIPPED)
        with pytest.raises(OperationNotPermittedError, match="locked"):
            await backend.update_shipping_address(shipped.reference, "1 New Street, Belfast")

    async def test_cannot_refund_more_than_was_paid(self, backend, dataset) -> None:
        delivered = next(o for o in dataset.orders if o.status is OrderStatus.DELIVERED)
        with pytest.raises(OperationNotPermittedError, match="exceeds the refundable"):
            await backend.issue_refund(delivered.reference, delivered.total_gbp + 1, "test")

    async def test_refunding_twice_exhausts_the_balance(self, backend, dataset) -> None:
        order = next(o for o in dataset.orders if o.status is OrderStatus.DELIVERED)
        half = round(order.total_gbp / 2, 2)
        await backend.issue_refund(order.reference, half, "first")
        await backend.issue_refund(order.reference, order.total_gbp - half, "second")
        assert (await backend.get_order(order.reference)).status is OrderStatus.REFUNDED
        with pytest.raises(OperationNotPermittedError):
            await backend.issue_refund(order.reference, 1.0, "third")

    async def test_cannot_refund_an_unpaid_order(self, backend, dataset) -> None:
        pending = next(o for o in dataset.orders if o.status is OrderStatus.PENDING_PAYMENT)
        with pytest.raises(OperationNotPermittedError, match="not been paid"):
            await backend.issue_refund(pending.reference, 10.0, "test")

    async def test_payment_link_is_deterministic_for_the_same_inputs(
        self, backend, dataset
    ) -> None:
        pending = next(o for o in dataset.orders if o.status is OrderStatus.PENDING_PAYMENT)
        a = await backend.create_payment_link(pending.reference, pending.total_gbp)
        b = await backend.create_payment_link(pending.reference, pending.total_gbp)
        assert a.url == b.url


class TestToolOutputs:
    async def test_list_payments_flags_a_genuine_duplicate(self, registry, context) -> None:
        result = await registry.execute("list_payments", {"order_ref": "ORD-400012"}, context)
        assert result.ok
        assert result.data["duplicate_suspected"] is True
        assert result.data["succeeded_count"] >= 2

    async def test_list_payments_does_not_invent_a_duplicate(self, registry, context) -> None:
        result = await registry.execute("list_payments", {"order_ref": "ORD-400037"}, context)
        assert result.ok
        assert result.data["duplicate_suspected"] is False
