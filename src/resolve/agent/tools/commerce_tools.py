"""The tools the agent can actually use.

Risk tiers are assigned by consequence, not by how often the tool is right:

  AUTO              read-only, or reversible with one click
  REQUIRES_APPROVAL moves money or changes physical fulfilment

`issue_refund` sits in the approval tier by default; the policy engine can
auto-approve it below a configured amount. That threshold is the only place in
the system where an autonomous agent is permitted to move money, and it is one
line of configuration a business owner can set (ADR-007).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from resolve.agent.tools.registry import Tool, ToolContext, ToolRegistry
from resolve.domain.enums import ActionRisk
from resolve.services.commerce import CommerceBackend

# --------------------------------------------------------------------------
# Argument schemas
# --------------------------------------------------------------------------


class OrderRefArgs(BaseModel):
    model_config = {"extra": "forbid"}
    order_ref: str = Field(description="The order reference, e.g. ORD-482913")


class RefundArgs(BaseModel):
    model_config = {"extra": "forbid"}
    order_ref: str = Field(description="The order reference to refund against")
    amount_gbp: float = Field(gt=0, description="Refund amount in GBP")
    reason: str = Field(max_length=300, description="Why the refund is being issued")


class PaymentLinkArgs(BaseModel):
    model_config = {"extra": "forbid"}
    order_ref: str
    amount_gbp: float | None = Field(
        default=None,
        description="Amount to charge. Defaults to the order's outstanding balance.",
    )


class AddressArgs(BaseModel):
    model_config = {"extra": "forbid"}
    order_ref: str
    new_address: str = Field(min_length=5, max_length=400)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def build_commerce_registry(commerce: CommerceBackend) -> ToolRegistry:
    async def lookup_order(args: OrderRefArgs, ctx: ToolContext) -> dict[str, Any]:
        order = await ctx.commerce.get_order(args.order_ref)
        ctx.scratch["order"] = order
        return {
            "reference": order.reference,
            "status": order.status.value,
            "customer_name": order.customer_name,
            "total_gbp": order.total_gbp,
            "refunded_gbp": order.refunded_gbp,
            "placed_at": order.placed_at.date().isoformat(),
            "shipping_address": order.shipping_address,
            "items": [
                {"name": line.name, "quantity": line.quantity, "sku": line.sku}
                for line in order.lines
            ],
        }

    async def get_tracking(args: OrderRefArgs, ctx: ToolContext) -> dict[str, Any]:
        order = await ctx.commerce.get_order(args.order_ref)
        return {
            "reference": order.reference,
            "status": order.status.value,
            "tracking_number": order.tracking_number,
            "carrier": order.carrier,
            "estimated_delivery": (
                order.estimated_delivery.date().isoformat() if order.estimated_delivery else None
            ),
        }

    async def list_payments(args: OrderRefArgs, ctx: ToolContext) -> dict[str, Any]:
        payments = await ctx.commerce.list_payments(args.order_ref)
        succeeded = [p for p in payments if p.status == "succeeded"]
        return {
            "reference": args.order_ref.upper(),
            "payments": [
                {
                    "id": p.id,
                    "amount_gbp": p.amount_gbp,
                    "status": p.status,
                    "created_at": p.created_at.isoformat(),
                    "last4": p.last4,
                }
                for p in payments
            ],
            "succeeded_count": len(succeeded),
            "duplicate_suspected": len(succeeded) > 1,
            "total_captured_gbp": round(sum(p.amount_gbp for p in succeeded), 2),
        }

    async def create_payment_link(args: PaymentLinkArgs, ctx: ToolContext) -> dict[str, Any]:
        order = await ctx.commerce.get_order(args.order_ref)
        amount = args.amount_gbp if args.amount_gbp is not None else order.total_gbp
        link = await ctx.commerce.create_payment_link(args.order_ref, amount)
        return {
            "payment_link": link.url,
            "amount_gbp": link.amount_gbp,
            "expires_at": link.expires_at.isoformat(),
            "order_ref": link.order_ref,
        }

    async def issue_refund(args: RefundArgs, ctx: ToolContext) -> dict[str, Any]:
        record = await ctx.commerce.issue_refund(args.order_ref, args.amount_gbp, args.reason)
        return {
            "refund_id": record.id,
            "order_ref": record.order_ref,
            "refund_amount_gbp": abs(record.amount_gbp),
            "status": record.status,
        }

    async def cancel_order(args: OrderRefArgs, ctx: ToolContext) -> dict[str, Any]:
        order = await ctx.commerce.cancel_order(args.order_ref)
        return {"reference": order.reference, "status": order.status.value}

    async def update_shipping_address(args: AddressArgs, ctx: ToolContext) -> dict[str, Any]:
        order = await ctx.commerce.update_shipping_address(args.order_ref, args.new_address)
        return {"reference": order.reference, "shipping_address": order.shipping_address}

    return ToolRegistry(
        [
            Tool(
                name="lookup_order",
                description="Fetch an order's status, items, totals and delivery address.",
                args_model=OrderRefArgs,
                risk=ActionRisk.AUTO,
                handler=lookup_order,
            ),
            Tool(
                name="get_tracking",
                description="Fetch carrier, tracking number and estimated delivery for an order.",
                args_model=OrderRefArgs,
                risk=ActionRisk.AUTO,
                handler=get_tracking,
            ),
            Tool(
                name="list_payments",
                description=(
                    "List every payment attempt against an order and flag suspected "
                    "duplicate charges."
                ),
                args_model=OrderRefArgs,
                risk=ActionRisk.AUTO,
                handler=list_payments,
            ),
            Tool(
                name="create_payment_link",
                description=(
                    "Generate a secure, time-limited link so the customer can complete "
                    "payment for an unpaid order."
                ),
                args_model=PaymentLinkArgs,
                risk=ActionRisk.AUTO,
                handler=create_payment_link,
            ),
            Tool(
                name="issue_refund",
                description="Refund money to the customer's original payment method.",
                args_model=RefundArgs,
                risk=ActionRisk.REQUIRES_APPROVAL,
                handler=issue_refund,
                approval_summary=lambda a: (
                    f"Refund £{a.amount_gbp:.2f} on {a.order_ref} — {a.reason}"
                ),
            ),
            Tool(
                name="cancel_order",
                description="Cancel an order that has not yet shipped.",
                args_model=OrderRefArgs,
                risk=ActionRisk.REQUIRES_APPROVAL,
                handler=cancel_order,
                approval_summary=lambda a: f"Cancel order {a.order_ref}",
            ),
            Tool(
                name="update_shipping_address",
                description="Change the delivery address on an order that has not yet shipped.",
                args_model=AddressArgs,
                risk=ActionRisk.REQUIRES_APPROVAL,
                handler=update_shipping_address,
                approval_summary=lambda a: (
                    f"Change delivery address on {a.order_ref} to: {a.new_address}"
                ),
            ),
        ]
    )
