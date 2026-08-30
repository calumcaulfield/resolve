"""Workflow definitions.

The ticket-resolution workflow is written as a linear sequence of named,
checkpointed steps. Kill the worker between any two of them and the run
resumes exactly where it stopped — it will not re-triage, will not re-call the
model, and above all will not re-issue a refund.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resolve.agent.loop import AgentRunner
from resolve.db.models import AgentRun, AgentStepRow, Approval
from resolve.db.models import Ticket as TicketRow
from resolve.db.session import session_scope
from resolve.domain.enums import ActorType, ApprovalOutcome, ReplyState, TicketStatus
from resolve.domain.schemas import AgentOutcome, Ticket
from resolve.llm.prompts import PROMPT_VERSION
from resolve.oversight import describe_amount, describe_resource
from resolve.workflow.engine import WorkflowContext, WorkflowFn


def _initial_reply_state(outcome: AgentOutcome) -> ReplyState:
    """Where a freshly drafted reply starts.

    An autonomous resolution sends immediately. Anything blocked on a human
    decision is explicitly *not* sent, and says so, rather than looking
    identical to one that was.
    """
    if outcome.reply is None:
        return ReplyState.NONE
    if outcome.status is TicketStatus.RESOLVED:
        return ReplyState.SENT
    if outcome.status is TicketStatus.AWAITING_APPROVAL:
        return ReplyState.AWAITING_APPROVAL
    return ReplyState.PROVISIONAL


RESOLVE_TICKET = "resolve_ticket"


def build_resolve_ticket_workflow(
    agent: AgentRunner,
    session_factory: async_sessionmaker[AsyncSession],
    provider_name: str,
) -> WorkflowFn:
    async def workflow(ctx: WorkflowContext) -> dict[str, Any]:
        ticket_id: str = str(ctx.payload["ticket_id"])

        # Step 1 — load. Checkpointed so a resumed run reads the same ticket.
        async def load() -> dict[str, Any]:
            async with session_scope(session_factory) as session:
                row = await session.get(TicketRow, ticket_id)
                if row is None:
                    raise LookupError(f"ticket {ticket_id} not found")
                return {
                    "id": row.id,
                    "external_id": row.external_id,
                    "channel": row.channel,
                    "from_email": row.from_email,
                    "subject": row.subject,
                    "body": row.body,
                }

        raw = await ctx.step("load_ticket", load)

        # Step 2 — the agent. The expensive, non-idempotent step, so it is
        # checkpointed: a crash after this point never pays for it twice.
        async def run_agent() -> dict[str, Any]:
            ticket = Ticket(
                id=raw["id"],
                external_id=raw["external_id"],
                channel=raw["channel"],
                from_email=raw["from_email"],
                subject=raw["subject"],
                body=raw["body"],
            )
            outcome = await agent.run(ticket)
            return outcome.model_dump(mode="json")

        outcome_payload = await ctx.step("run_agent", run_agent)
        outcome = AgentOutcome.model_validate(outcome_payload)

        # Step 3 — persist the run, its steps and any approval requests.
        async def persist() -> dict[str, Any]:
            async with session_scope(session_factory) as session:
                run = AgentRun(
                    ticket_id=ticket_id,
                    status=outcome.status.value,
                    intent=outcome.intent.value if outcome.intent else None,
                    escalation_reason=outcome.escalation_reason,
                    reply_subject=outcome.reply.subject if outcome.reply else None,
                    reply_body=outcome.reply.body if outcome.reply else None,
                    reply_state=_initial_reply_state(outcome).value,
                    grounded=outcome.verification.grounded if outcome.verification else None,
                    citation_count=len(outcome.reply.citations) if outcome.reply else 0,
                    cost_usd=outcome.total_cost_usd,
                    tokens_in=outcome.total_tokens_in,
                    tokens_out=outcome.total_tokens_out,
                    duration_ms=outcome.duration_ms,
                    budget_exceeded=outcome.budget_exceeded,
                    prompt_version=PROMPT_VERSION,
                    provider=provider_name,
                )
                session.add(run)
                await session.flush()

                for step in outcome.steps:
                    session.add(
                        AgentStepRow(
                            run_id=run.id,
                            index=step.index,
                            kind=step.kind,
                            summary=step.summary,
                            detail=step.detail,
                            # Safety findings are the agent observing its own
                            # input rather than reasoning, so they are attributed
                            # to the system; everything else in a run is the
                            # agent's own work.
                            actor_type=(
                                ActorType.SYSTEM.value
                                if step.kind in {"safety", "redact"}
                                else ActorType.AGENT.value
                            ),
                            actor=None,
                            duration_ms=step.duration_ms,
                            cost_usd=step.cost_usd,
                            tokens_in=step.tokens_in,
                            tokens_out=step.tokens_out,
                        )
                    )

                for step in outcome.steps:
                    if step.kind != "approval":
                        continue
                    arguments = dict(step.detail.get("arguments") or {})
                    session.add(
                        Approval(
                            id=str(step.detail.get("approval_id")),
                            ticket_id=ticket_id,
                            run_id=run.id,
                            tool=str(step.detail.get("tool", "")),
                            arguments=arguments,
                            rationale=str(step.detail.get("summary") or step.summary),
                            risk=str(step.detail.get("risk") or "requires_approval"),
                            # Denormalised at request time so the audit trail
                            # stays readable and queryable even if a tool's
                            # argument shape changes later.
                            resource=describe_resource(arguments),
                            amount_gbp=describe_amount(arguments),
                            outcome=ApprovalOutcome.PENDING.value,
                        )
                    )

                ticket_row = await session.get(TicketRow, ticket_id)
                if ticket_row is not None:
                    ticket_row.status = outcome.status.value
                    ticket_row.intent = outcome.intent.value if outcome.intent else None
                    ticket_row.urgency = outcome.urgency.value if outcome.urgency else None
                    ticket_row.order_ref = outcome.order_ref

                return {"run_id": run.id, "status": outcome.status.value}

        persisted = await ctx.step("persist_outcome", persist)

        return {
            "ticket_id": ticket_id,
            "run_id": persisted["run_id"],
            "status": outcome.status.value,
            "cost_usd": outcome.total_cost_usd,
        }

    return workflow
