"""Persistence model.

Design notes that matter more than the columns:

* **Nothing operationally important lives in process memory.** The audit of
  the original payment control plane found its entire queue, its webhook map
  and its result cache were JavaScript objects — a deploy destroyed queued
  work silently. Here, tickets, runs, steps, approvals, workflow state and
  outbound messages are all rows.

* **`idempotency_keys`** makes retried submissions safe. A client that times
  out and retries gets the original response, not a duplicate ticket.

* **`outbox`** implements the transactional outbox pattern: an outbound
  webhook is written in the *same transaction* as the state change that
  caused it, then delivered by a separate dispatcher. There is no window in
  which the state changed but the notification was lost.

* **`agent_steps`** is an append-only audit of every decision the agent made,
  including the ones a human overrode. That is what makes an AI system
  answerable after the fact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from resolve.db.base import Base, new_id, utcnow


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="email")
    from_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    urgency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    order_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    runs: Mapped[list[AgentRun]] = relationship(back_populates="ticket", cascade="all, delete")

    __table_args__ = (
        # One ticket per inbound message, even if the webhook fires twice.
        UniqueConstraint("external_id", name="uq_tickets_external_id"),
        Index("ix_tickets_status_created", "status", "created_at"),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reply_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Where the drafted reply is in its lifecycle. A draft is not a sent
    #: message; without this column the two are indistinguishable, and a reply
    #: describing a refund that a human later refused would still be shown as
    #: the outcome. See `resolve.domain.enums.ReplyState`.
    reply_state: Mapped[str] = mapped_column(String(24), default="none", index=True)
    reply_state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    grounded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    citation_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    budget_exceeded: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    provider: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="runs")
    steps: Mapped[list[AgentStepRow]] = relationship(
        back_populates="run", cascade="all, delete", order_by="AgentStepRow.index"
    )


class AgentStepRow(Base):
    __tablename__ = "agent_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Who caused this step. Human decisions are appended to the same trace as
    #: the agent's own steps, so a reviewer reads one ordered story rather than
    #: correlating two timelines by hand.
    actor_type: Mapped[str] = mapped_column(String(16), default="agent")
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(String(24), default="requires_approval")
    #: The thing the action would affect, e.g. `ORD-400001`. Denormalised from
    #: `arguments` at request time so the audit trail stays readable even if
    #: the tool's argument shape changes later.
    resource: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: Denormalised monetary value where the action moves money, so financial
    #: decisions can be found and totalled without parsing JSON.
    amount_gbp: Mapped[float | None] = mapped_column(Float, nullable=True)

    decision: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_by_type: Mapped[str] = mapped_column(String(16), default="human")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: What happened, as distinct from what was decided. A rejected action has
    #: outcome `not_executed`; an approved one that the commerce backend
    #: refused has `execution_failed`. `executed` is kept as a plain boolean
    #: for the common query and is always consistent with `outcome`.
    outcome: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_approvals_decision_created", "decision", "created_at"),)


class WorkflowRun(Base):
    """One durable execution of a named workflow."""

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="run", cascade="all, delete", order_by="WorkflowStep.created_at"
    )

    __table_args__ = (
        # A workflow is keyed by (name, correlation_id), so replaying the same
        # event never starts a second run.
        UniqueConstraint("workflow", "correlation_id", name="uq_workflow_runs_correlation"),
    )


class WorkflowStep(Base):
    """A single checkpointed step. Completed steps are never re-executed."""

    __tablename__ = "workflow_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[WorkflowRun] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("run_id", "name", name="uq_workflow_steps_run_name"),)


class OutboxMessage(Base):
    """Transactional outbox for outbound webhooks."""

    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String(64), index=True)
    target_url: Mapped[str] = mapped_column(String(2048))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_outbox_status_next", "status", "next_attempt_at"),)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128))
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyChunkRow(Base):
    """Retrievable policy text. `embedding` is `vector(512)` on Postgres and a
    JSON array on SQLite, which is what lets the test suite run without
    Postgres while production uses a real ANN index."""

    __tablename__ = "policy_chunks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[Any | None] = mapped_column(JSON, nullable=True)
