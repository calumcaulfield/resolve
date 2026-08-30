"""Human authorisation, execution and audit.

Resolve keeps five concerns deliberately separate, and this module owns the
middle three:

    AI decision-making    the agent proposes an action        (agent/loop.py)
    policy enforcement    the tier decides if a human is needed (agent/policy.py)
    human authorisation   a person approves or refuses         ← here
    execution             the action runs, or provably does not ← here
    auditability          what was decided, by whom, and why    ← here

Why this is not inline in the API route
---------------------------------------
It was, and it was wrong in a way that mattered. Recording a rejection used to
mean setting `decision = "rejected"` on one row. Everything downstream of that
fact was left stale:

* the ticket's trace contained no human-decision event at all, so the timeline
  simply stopped at "queued for human approval";
* `escalation_reason` still read "1 action(s) require human approval", which is
  no longer true once a human has decided;
* the drafted reply — written on the assumption the refund *would* be issued —
  was still presented as the outcome, telling the reader the customer had been
  told something that never happened.

None of that is a rendering problem, so none of it can be fixed in the console.
A rejection is a domain event with consequences for the run, the reply and the
ticket, and those consequences belong here.

The lifecycle this produces
---------------------------
    APPROVAL REQUESTED
      → HUMAN DECISION: REJECTED        (durable, attributed, timestamped)
      → ACTION NOT EXECUTED             (outcome, distinct from decision)
      → PROVISIONAL DRAFT VOIDED        (reply_state, retained for audit)
      → TICKET ESCALATED FOR MANUAL HANDLING
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resolve.agent.tools.registry import ToolContext, ToolRegistry
from resolve.db.models import AgentRun, AgentStepRow, Approval
from resolve.db.models import Ticket as TicketRow
from resolve.domain.enums import (
    ActorType,
    ApprovalDecision,
    ApprovalOutcome,
    ReplyState,
    TicketStatus,
)
from resolve.logging import get_logger
from resolve.services.commerce import CommerceBackend

log = get_logger(__name__)


class ApprovalNotFoundError(Exception):
    pass


class AlreadyDecidedError(Exception):
    """Idempotency guard: an approval may be decided exactly once.

    Kept as its own type so the API can answer 409 rather than 500, and so the
    guard cannot be accidentally softened into an overwrite.
    """

    def __init__(self, approval_id: str, decision: str) -> None:
        super().__init__(f"approval {approval_id} was already {decision}")
        self.approval_id = approval_id
        self.decision = decision


#: Arguments that name the thing an action affects, most specific first.
_RESOURCE_KEYS = ("order_ref", "reference", "contract_id", "payment_id")


def describe_resource(arguments: dict[str, Any]) -> str | None:
    """Pull the affected resource out of a tool's arguments for the audit trail."""
    for key in _RESOURCE_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def describe_amount(arguments: dict[str, Any]) -> float | None:
    value = arguments.get("amount_gbp")
    if isinstance(value, int | float):
        return round(float(value), 2)
    return None


@dataclass
class DecisionResult:
    approval: Approval
    ticket_status: TicketStatus
    reply_state: ReplyState
    escalation_reason: str | None
    executed: bool
    outcome: ApprovalOutcome


class HumanDecisionService:
    """Applies a human decision and everything that follows from it."""

    def __init__(self, registry: ToolRegistry, commerce: CommerceBackend) -> None:
        self.registry = registry
        self.commerce = commerce

    # ------------------------------------------------------------------
    # trace
    # ------------------------------------------------------------------

    @staticmethod
    async def _next_step_index(session: AsyncSession, run_id: str) -> int:
        rows = (
            (await session.execute(select(AgentStepRow.index).where(AgentStepRow.run_id == run_id)))
            .scalars()
            .all()
        )
        return (max(rows) + 1) if rows else 0

    async def _append_step(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        kind: str,
        summary: str,
        detail: dict[str, Any],
        actor_type: ActorType,
        actor: str | None = None,
    ) -> None:
        """Append to the *same* trace the agent wrote.

        One ordered timeline, with every entry attributed. A reviewer should be
        able to read a ticket top to bottom and see exactly where the machine
        stopped and a person took over.
        """
        session.add(
            AgentStepRow(
                run_id=run_id,
                index=await self._next_step_index(session, run_id),
                kind=kind,
                summary=summary,
                detail=detail,
                actor_type=actor_type.value,
                actor=actor,
            )
        )

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------

    async def decide(
        self,
        session: AsyncSession,
        approval_id: str,
        *,
        approve: bool,
        decided_by: str,
        note: str | None = None,
        actor_type: ActorType = ActorType.HUMAN,
    ) -> DecisionResult:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalNotFoundError(approval_id)
        if approval.decision != ApprovalDecision.PENDING.value:
            raise AlreadyDecidedError(approval_id, approval.decision)

        now = datetime.now(UTC)
        decision = ApprovalDecision.APPROVED if approve else ApprovalDecision.REJECTED

        approval.decision = decision.value
        approval.decided_by = decided_by
        approval.decided_by_type = actor_type.value
        approval.decided_at = now
        approval.note = note

        # 1 — the decision itself, before anything is attempted.
        await self._append_step(
            session,
            approval.run_id or "",
            kind="human_decision",
            summary=(
                f"Approved by {decided_by}: {approval.rationale}"
                if approve
                else f"Rejected by {decided_by}: {approval.rationale}"
            ),
            detail={
                "decision": decision.value,
                "action": approval.tool,
                "resource": approval.resource,
                "amount_gbp": approval.amount_gbp,
                "risk": approval.risk,
                "arguments": approval.arguments,
                "reason": note,
                "actor": decided_by,
                "actor_type": actor_type.value,
                "decided_at": now.isoformat(),
                "approval_id": approval.id,
            },
            actor_type=actor_type,
            actor=decided_by,
        )

        # 2 — the consequence.
        if approve:
            result = await self.registry.execute(
                approval.tool,
                approval.arguments,
                ToolContext(
                    commerce=self.commerce,
                    ticket_id=approval.ticket_id,
                    correlation_id=approval_id,
                ),
            )
            approval.executed = result.ok
            approval.execution_result = result.model_dump(mode="json")
            approval.outcome = (
                ApprovalOutcome.EXECUTED if result.ok else ApprovalOutcome.EXECUTION_FAILED
            ).value
            await self._append_step(
                session,
                approval.run_id or "",
                kind="execute",
                summary=(
                    f"Executed {approval.tool}"
                    + (f" on {approval.resource}" if approval.resource else "")
                    if result.ok
                    else f"{approval.tool} failed: {result.error}"
                ),
                detail={
                    "tool": approval.tool,
                    "resource": approval.resource,
                    "ok": result.ok,
                    "data": result.data,
                    "error": result.error,
                    "authorised_by": decided_by,
                },
                actor_type=ActorType.SYSTEM,
                actor=approval.tool,
            )
        else:
            approval.executed = False
            approval.execution_result = None
            approval.outcome = ApprovalOutcome.NOT_EXECUTED.value
            # Stated explicitly and stored, so no reader has to infer it from
            # an absence. "Nothing happened" is a fact worth recording.
            await self._append_step(
                session,
                approval.run_id or "",
                kind="not_executed",
                summary=(
                    f"{approval.tool} was not executed"
                    + (f" on {approval.resource}" if approval.resource else "")
                ),
                detail={
                    "tool": approval.tool,
                    "resource": approval.resource,
                    "reason": note or "rejected by a human reviewer",
                    "rejected_by": decided_by,
                },
                actor_type=ActorType.SYSTEM,
                actor=approval.tool,
            )

        # 3 — recompute the ticket and the reply from *every* approval on this
        #     ticket, not from this one decision. Deciding each approval in
        #     isolation meant the last decision won: approve one, reject
        #     another, and the ticket could end up "resolved" with an action
        #     a human had explicitly refused.
        outcome = await self._reconcile(session, approval, decided_by)

        log.info(
            "oversight.decided",
            approval_id=approval_id,
            decision=decision.value,
            outcome=approval.outcome,
            action=approval.tool,
            resource=approval.resource,
            actor=decided_by,
            ticket_status=outcome.ticket_status.value,
            reply_state=outcome.reply_state.value,
        )
        return outcome

    # ------------------------------------------------------------------
    # reconciliation
    # ------------------------------------------------------------------

    async def _reconcile(
        self, session: AsyncSession, approval: Approval, decided_by: str
    ) -> DecisionResult:
        siblings = (
            (
                await session.execute(
                    select(Approval).where(Approval.ticket_id == approval.ticket_id)
                )
            )
            .scalars()
            .all()
        )
        pending = [a for a in siblings if a.decision == ApprovalDecision.PENDING.value]
        rejected = [a for a in siblings if a.decision == ApprovalDecision.REJECTED.value]
        failed = [a for a in siblings if a.outcome == ApprovalOutcome.EXECUTION_FAILED.value]

        run = await session.get(AgentRun, approval.run_id) if approval.run_id else None
        has_reply = bool(run and run.reply_body)

        if pending:
            status = TicketStatus.AWAITING_APPROVAL
            reply_state = ReplyState.AWAITING_APPROVAL if has_reply else ReplyState.NONE
            reason = f"{len(pending)} action(s) still awaiting a human decision."
            reply_reason = None
        elif rejected:
            # The draft was written on the assumption the action would happen.
            # With the action refused, its claims are no longer true, so it is
            # voided rather than held: it must never be sent as-is.
            status = TicketStatus.ESCALATED
            reply_state = ReplyState.VOIDED if has_reply else ReplyState.NONE
            actions = ", ".join(sorted({a.tool for a in rejected}))
            reason = (
                f"Escalated for manual handling: {len(rejected)} proposed action(s) "
                f"({actions}) were rejected by a human reviewer, so the drafted "
                "reply is no longer accurate and was not sent."
            )
            reply_reason = (
                "Voided because an action this reply describes was rejected by a "
                "human reviewer and never executed."
            )
        elif failed:
            # Approved, attempted, refused downstream. The draft may be partly
            # salvageable, so a person looks at it rather than it being binned.
            status = TicketStatus.ESCALATED
            reply_state = ReplyState.HELD if has_reply else ReplyState.NONE
            reason = (
                f"Escalated for manual handling: {len(failed)} approved action(s) "
                "could not be executed by the downstream system."
            )
            reply_reason = "Held for review: an approved action failed to execute."
        else:
            status = TicketStatus.RESOLVED
            reply_state = ReplyState.SENT if has_reply else ReplyState.NONE
            reason = None
            reply_reason = None

        if run is not None:
            run.status = status.value
            run.escalation_reason = reason
            run.reply_state = reply_state.value
            run.reply_state_reason = reply_reason

            if not pending:
                await self._append_step(
                    session,
                    run.id,
                    kind="escalate" if status is TicketStatus.ESCALATED else "resolve",
                    summary=(
                        reason
                        if reason
                        else "All required actions authorised and executed; reply sent."
                    ),
                    detail={
                        "ticket_status": status.value,
                        "reply_state": reply_state.value,
                        "approvals_total": len(siblings),
                        "approved": len(
                            [a for a in siblings if a.decision == ApprovalDecision.APPROVED.value]
                        ),
                        "rejected": len(rejected),
                        "execution_failed": len(failed),
                        "decided_by": decided_by,
                    },
                    actor_type=ActorType.SYSTEM,
                    actor="oversight",
                )

        ticket = await session.get(TicketRow, approval.ticket_id)
        if ticket is not None:
            ticket.status = status.value

        return DecisionResult(
            approval=approval,
            ticket_status=status,
            reply_state=reply_state,
            escalation_reason=reason,
            executed=approval.executed,
            outcome=ApprovalOutcome(approval.outcome),
        )
