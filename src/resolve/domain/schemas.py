"""Typed domain models.

Everything crossing a boundary — HTTP, the event bus, the LLM — is one of
these. Nothing in this system passes a bare dict around.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from resolve.domain.enums import (
    ActionRisk,
    ApprovalDecision,
    Channel,
    Intent,
    OrderStatus,
    TicketStatus,
    Urgency,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=False)


# --------------------------------------------------------------------------
# Inbound
# --------------------------------------------------------------------------


class InboundMessage(Base):
    """A raw customer message as it arrives, before any processing."""

    external_id: str
    channel: Channel = Channel.EMAIL
    from_email: str
    subject: str = ""
    body: str
    received_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message body must not be empty")
        return v


class Ticket(Base):
    id: str = Field(default_factory=_new_id)
    external_id: str
    channel: Channel
    from_email: str
    subject: str
    body: str
    status: TicketStatus = TicketStatus.RECEIVED
    intent: Intent | None = None
    urgency: Urgency | None = None
    order_ref: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# LLM structured outputs
#
# Each of these is the *schema the model must produce*. They are deliberately
# small: narrow schemas validate more reliably than wide ones.
# --------------------------------------------------------------------------


class TriageResult(Base):
    """Stage 1 output — produced by the cheap model."""

    intent: Intent
    urgency: Urgency
    confidence: float = Field(ge=0.0, le=1.0)
    order_ref: str | None = Field(
        default=None,
        description="Order reference extracted from the message, if one is present.",
    )
    summary: str = Field(max_length=400)
    customer_sentiment: str = Field(default="neutral")

    @field_validator("order_ref")
    @classmethod
    def _clean_ref(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip().upper()
        return cleaned or None


class PlannedAction(Base):
    """Stage 3 output — one tool the agent intends to call."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(max_length=500)


class AgentPlan(Base):
    actions: list[PlannedAction] = Field(default_factory=list, max_length=6)
    reasoning: str = Field(default="", max_length=1500)
    needs_human: bool = False
    human_reason: str | None = None


class Citation(Base):
    """A pointer back to the retrieved policy chunk that supports a claim.

    Replies that cite nothing are not sent. See ADR-005.
    """

    document_id: str
    chunk_id: str
    title: str
    quote: str = Field(max_length=500)


class DraftedReply(Base):
    """Stage 4 output — the customer-facing message."""

    subject: str = Field(max_length=200)
    body: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    tone: str = "professional"


class VerificationResult(Base):
    """Stage 5 output — a second model pass checking the first one's work."""

    grounded: bool
    hallucination_risk: float = Field(ge=0.0, le=1.0)
    unsupported_claims: list[str] = Field(default_factory=list)
    policy_violations: list[str] = Field(default_factory=list)
    verdict: str = Field(default="", max_length=600)


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------


class PolicyDocument(Base):
    id: str
    title: str
    category: str
    body: str
    version: str = "1"


class Chunk(Base):
    id: str
    document_id: str
    title: str
    text: str
    ordinal: int = 0
    embedding: list[float] | None = None


class RetrievedChunk(Base):
    chunk: Chunk
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    score: float = 0.0

    @property
    def citation(self) -> Citation:
        return Citation(
            document_id=self.chunk.document_id,
            chunk_id=self.chunk.id,
            title=self.chunk.title,
            quote=self.chunk.text[:480],
        )


# --------------------------------------------------------------------------
# Agent execution record
# --------------------------------------------------------------------------


class ToolResult(Base):
    tool: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    risk: ActionRisk = ActionRisk.AUTO


class AgentStep(Base):
    """One observable step of the agent loop. The console renders these as a
    timeline so a human can see exactly what the agent did and why."""

    index: int
    kind: str  # triage | retrieve | plan | act | draft | verify | escalate
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_now)
    duration_ms: float = 0.0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


class CostRecord(Base):
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cached: bool = False


class AgentOutcome(Base):
    """The complete, auditable result of one ticket passing through the agent."""

    ticket_id: str
    status: TicketStatus
    intent: Intent | None = None
    urgency: Urgency | None = None
    #: The order this ticket is about, as extracted during triage. Carried on
    #: the outcome so it can be persisted to the ticket row — without it the
    #: reference is only visible inside the triage step's detail blob, and the
    #: `order_ref` column and index are dead weight.
    order_ref: str | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    reply: DraftedReply | None = None
    verification: VerificationResult | None = None
    escalation_reason: str | None = None
    pending_approvals: list[str] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    duration_ms: float = 0.0
    budget_exceeded: bool = False

    @property
    def auto_resolved(self) -> bool:
        return self.status is TicketStatus.RESOLVED


# --------------------------------------------------------------------------
# Business objects the tools operate on
# --------------------------------------------------------------------------


class OrderLine(Base):
    sku: str
    name: str
    quantity: int
    unit_price_gbp: float


class Order(Base):
    reference: str
    customer_email: str
    customer_name: str
    status: OrderStatus
    total_gbp: float
    currency: str = "GBP"
    placed_at: datetime
    lines: list[OrderLine] = Field(default_factory=list)
    shipping_address: str = ""
    tracking_number: str | None = None
    carrier: str | None = None
    estimated_delivery: datetime | None = None
    payment_reference: str | None = None
    refunded_gbp: float = 0.0


class ApprovalRequest(Base):
    id: str = Field(default_factory=_new_id)
    ticket_id: str
    tool: str
    arguments: dict[str, Any]
    rationale: str
    risk: ActionRisk
    decision: ApprovalDecision = ApprovalDecision.PENDING
    requested_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None
