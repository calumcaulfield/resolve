"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field

from resolve.domain.evaluation import EvalReport


class IngestRequest(BaseModel):
    model_config = {"extra": "forbid"}

    external_id: str = Field(max_length=128, description="Stable id from the source system")
    from_email: str = Field(max_length=320)
    subject: str = Field(default="", max_length=512)
    body: str = Field(min_length=1)
    channel: str = Field(default="email")


class IngestResponse(BaseModel):
    ticket_id: str
    status: str
    duplicate: bool = False


class StepView(BaseModel):
    index: int
    kind: str
    summary: str
    detail: dict[str, Any]
    duration_ms: float
    cost_usd: float
    #: agent | human | system — lets the console distinguish AI reasoning from
    #: a person's decision from an external tool action in one timeline.
    actor_type: str = "agent"
    actor: str | None = None
    created_at: datetime | None = None


class RunView(BaseModel):
    id: str
    status: str
    intent: str | None
    escalation_reason: str | None
    reply_subject: str | None
    reply_body: str | None
    reply_state: str = "none"
    reply_state_reason: str | None = None
    grounded: bool | None
    citation_count: int
    cost_usd: float
    tokens_in: int
    tokens_out: int
    duration_ms: float
    provider: str
    prompt_version: str
    created_at: datetime
    steps: list[StepView] = Field(default_factory=list)


class TicketView(BaseModel):
    id: str
    external_id: str
    from_email: str
    subject: str
    body: str
    status: str
    intent: str | None
    urgency: str | None
    order_ref: str | None
    created_at: datetime


class TicketDetail(TicketView):
    runs: list[RunView] = Field(default_factory=list)


class ApprovalView(BaseModel):
    id: str
    ticket_id: str
    run_id: str | None = None
    tool: str
    arguments: dict[str, Any]
    rationale: str
    risk: str
    resource: str | None = None
    amount_gbp: float | None = None
    decision: str
    decided_by: str | None = None
    decided_by_type: str = "human"
    decided_at: datetime | None = None
    note: str | None = None
    #: What happened, as opposed to what was decided. See `ApprovalOutcome`.
    outcome: str = "pending"
    executed: bool
    execution_result: dict[str, Any] | None
    created_at: datetime


class ApprovalContext(BaseModel):
    """Everything a reviewer needs to decide without opening another tab.

    An approval queue that shows only `{"order_ref": "...", "amount_gbp": ...}`
    asks a human to rubber-stamp a decision they cannot actually evaluate.
    """

    approval: ApprovalView
    ticket_subject: str = ""
    customer_email: str = ""
    customer_message: str = ""
    intent: str | None = None
    #: The agent's own reasoning for proposing this action.
    ai_rationale: str = ""
    #: Policy passages the agent retrieved, so the reviewer can check the basis.
    policy_citations: list[dict[str, str]] = Field(default_factory=list)
    #: The reply that will be sent if this is approved — and voided if not.
    proposed_reply_subject: str | None = None
    proposed_reply_body: str | None = None
    reply_state: str = "none"


class DecisionResultView(BaseModel):
    """The consequences of a decision, so the caller need not re-fetch."""

    approval: ApprovalView
    ticket_id: str
    ticket_status: str
    reply_state: str
    reply_state_reason: str | None = None
    escalation_reason: str | None = None
    executed: bool
    outcome: str


class OversightMetrics(BaseModel):
    """Human-oversight statistics.

    Deliberately separate from `/v1/evals/latest`. Evaluation answers "did the
    AI behave according to the golden set?"; this answers "what did humans do
    about the actions it proposed on real traffic?". Mixing them would let a
    good evaluation score paper over an operator rejecting most proposals.
    """

    decisions_total: int
    pending: int
    approved: int
    rejected: int
    execution_failed: int
    approval_rate: float
    rejection_rate: float
    #: Share of proposed privileged actions a human refused. The headline
    #: measure of how far the agent's judgement is trusted in practice.
    override_rate: float
    by_action: list[ActionOversight] = Field(default_factory=list)
    total_value_approved_gbp: float = 0.0
    total_value_rejected_gbp: float = 0.0
    #: Null when nothing has been decided yet — never zero, which would read
    #: as "instant", and never invented.
    median_time_to_decision_seconds: float | None = None


class ActionOversight(BaseModel):
    action: str
    proposed: int
    approved: int
    rejected: int
    pending: int
    approval_rate: float


class AuditEvent(BaseModel):
    """One human decision, for the activity stream."""

    approval_id: str
    ticket_id: str
    action: str
    resource: str | None
    amount_gbp: float | None
    risk: str
    decision: str
    outcome: str
    actor: str | None
    actor_type: str
    note: str | None
    decided_at: datetime
    ticket_subject: str = ""


class ApprovalDecisionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    approve: bool
    decided_by: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class AnalyticsResponse(BaseModel):
    tickets_total: int
    by_status: dict[str, int]
    by_intent: dict[str, int]
    auto_resolution_rate: float
    approval_rate: float
    escalation_rate: float
    groundedness_rate: float
    total_cost_usd: float
    mean_cost_usd: float
    mean_duration_ms: float
    pending_approvals: int
    #: Modelled, not measured. Assumptions are returned alongside the figure.
    projected_hours_saved: float
    assumptions: dict[str, float]


class EvalReportResponse(BaseModel):
    """The latest evaluation report, plus where it came from.

    `source` and `available_providers` are included so the console can say
    *which* run it is showing rather than presenting numbers without
    provenance. A metric with no attached configuration is not reproducible.
    """

    report: EvalReport
    source: str
    available_providers: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
    provider: str
    environment: str
    checks: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "AnalyticsResponse",
    "ApprovalDecisionRequest",
    "ApprovalView",
    "EmailStr",
    "EvalReportResponse",
    "HealthResponse",
    "IngestRequest",
    "IngestResponse",
    "RunView",
    "StepView",
    "TicketDetail",
    "TicketView",
]
