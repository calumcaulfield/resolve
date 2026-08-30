"""Domain vocabulary.

These enums are the contract between the LLM and the rest of the system. The
model is never allowed to invent a value: every structured response is parsed
into a Pydantic model whose fields are typed with these enums, and a response
that does not validate is repaired or rejected (see llm/client.py).
"""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    EMAIL = "email"
    WEBFORM = "webform"
    CHAT = "chat"


class Intent(StrEnum):
    """The taxonomy the triage stage classifies into.

    Derived from the actual shape of e-commerce support volume: order status
    and delivery questions dominate, payment problems are the expensive tail.
    """

    ORDER_STATUS = "order_status"
    DELIVERY_ISSUE = "delivery_issue"
    PAYMENT_FAILED = "payment_failed"
    DUPLICATE_CHARGE = "duplicate_charge"
    REFUND_REQUEST = "refund_request"
    CANCEL_ORDER = "cancel_order"
    CHANGE_ADDRESS = "change_address"
    PRODUCT_QUESTION = "product_question"
    COMPLAINT = "complaint"
    OTHER = "other"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TicketStatus(StrEnum):
    RECEIVED = "received"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


class ActionRisk(StrEnum):
    """Risk tier of a tool.

    The tier — not the model's confidence — decides whether a human is
    required. A very confident model asking for a £400 refund still stops at
    the approval queue.
    """

    AUTO = "auto"
    """Read-only or trivially reversible. Executed without asking."""

    REQUIRES_APPROVAL = "requires_approval"
    """Moves money or changes fulfilment. Queued for a human."""

    FORBIDDEN = "forbidden"
    """Registered so the model can be told 'no' explicitly, never executable."""


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class WorkflowStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalDecision(StrEnum):
    """What a human decided about a proposed action."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalOutcome(StrEnum):
    """What actually *happened* to the action, which is not the same question.

    `decision` records human intent; `outcome` records the consequence. Keeping
    them apart matters: "a human approved this" and "and it worked" are
    different facts, and an audit trail that conflates them cannot answer
    either one honestly.
    """

    PENDING = "pending"
    """Awaiting a decision. Nothing has been attempted."""

    EXECUTED = "executed"
    """Approved and the tool call succeeded."""

    EXECUTION_FAILED = "execution_failed"
    """Approved, attempted, and the downstream system refused it."""

    NOT_EXECUTED = "not_executed"
    """Rejected or expired. **The action never ran.**"""


class ActorType(StrEnum):
    """Who caused an event.

    Every entry in a ticket's trace is attributed. Without this the timeline
    cannot distinguish "the model decided" from "a person decided", which is
    precisely the distinction an oversight surface exists to show.
    """

    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


class ReplyState(StrEnum):
    """The lifecycle of the drafted customer reply.

    This exists because a draft is not a sent message, and the difference is
    invisible unless it is modelled. A reply written on the assumption that a
    refund would be issued must never be presented as an outcome once a human
    has refused that refund.
    """

    NONE = "none"
    """No reply was drafted (the run escalated before drafting)."""

    PROVISIONAL = "provisional"
    """Drafted, verified, but not yet released."""

    AWAITING_APPROVAL = "awaiting_approval"
    """Drafted, and blocked on a human decision about an action it describes."""

    SENT = "sent"
    """Released to the customer."""

    VOIDED = "voided"
    """Discarded: an action it depends on was rejected, so its claims are
    no longer true. Retained for audit, never presented as an outcome."""

    HELD = "held"
    """Not sent, and not discarded: needs a human to look at it. Used when an
    approved action failed downstream, so the draft may be partly salvageable."""
