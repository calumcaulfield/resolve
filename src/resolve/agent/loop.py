"""The agent loop.

Five stages, each observable, each bounded:

    triage → retrieve → plan → act → draft → verify

Every stage appends an `AgentStep` carrying its cost, duration and payload, so
the console can render exactly what happened and a human reviewing an
escalation can see the agent's reasoning rather than a black box.

Three invariants hold for every run:

1. **The budget is a ceiling, not a target.** If spend would exceed the
   per-ticket budget, the run stops and escalates. It never silently
   truncates its own reasoning and answers anyway.
2. **Nothing consequential happens without passing the policy engine.** The
   loop cannot execute a tool it did not first submit to `PolicyEngine`.
3. **A reply that is not grounded is not sent.** Verification failure routes
   to a human with the draft attached, which is still faster than writing
   from scratch — but it is not an automated resolution and is not counted
   as one.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from resolve.agent.policy import PolicyEngine, PolicyOutcome
from resolve.agent.tools.registry import ToolContext, ToolRegistry, dumps
from resolve.config import AgentSettings
from resolve.domain.enums import ActionRisk, TicketStatus
from resolve.domain.schemas import (
    AgentOutcome,
    AgentPlan,
    AgentStep,
    ApprovalRequest,
    DraftedReply,
    RetrievedChunk,
    Ticket,
    ToolResult,
    TriageResult,
    VerificationResult,
)
from resolve.llm.base import StructuredOutputError, Usage
from resolve.llm.client import LLMClient, ModelTier
from resolve.llm.prompts import (
    draft_reply_prompt,
    plan_prompt,
    triage_prompt,
    verify_prompt,
)
from resolve.logging import get_logger
from resolve.rag.store import HybridRetriever
from resolve.safety.injection import scan_for_injection
from resolve.safety.pii import redact
from resolve.services.commerce import CommerceBackend

log = get_logger(__name__)

#: Read-only tools the loop may run itself to build context before planning.
_CONTEXT_TOOL = "lookup_order"


@dataclass
class _Run:
    """Mutable state for one ticket. Never shared between tickets."""

    ticket: Ticket
    started: float
    steps: list[AgentStep] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    approvals: list[ApprovalRequest] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def add_step(
        self,
        kind: str,
        summary: str,
        detail: dict[str, Any] | None = None,
        *,
        started: float | None = None,
        cost: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.steps.append(
            AgentStep(
                index=len(self.steps),
                kind=kind,
                summary=summary,
                detail=detail or {},
                started_at=datetime.now(UTC),
                duration_ms=round(
                    (time.perf_counter() - (started or time.perf_counter())) * 1000, 3
                ),
                cost_usd=round(cost, 8),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        )
        self.cost_usd = round(self.cost_usd + cost, 8)
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out


class AgentRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        retriever: HybridRetriever,
        registry: ToolRegistry,
        commerce: CommerceBackend,
        policy: PolicyEngine,
        settings: AgentSettings | None = None,
        budget_usd: float = 0.25,
        redact_pii: bool = True,
    ) -> None:
        self.llm = llm
        self.retriever = retriever
        self.registry = registry
        self.commerce = commerce
        self.policy = policy
        self.settings = settings or AgentSettings()
        self.budget_usd = budget_usd
        self.redact_pii = redact_pii

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------

    async def run(self, ticket: Ticket) -> AgentOutcome:
        run = _Run(ticket=ticket, started=time.perf_counter())
        log.info("agent.start", ticket_id=ticket.id, subject=ticket.subject[:80])

        redaction = redact(ticket.body) if self.redact_pii else None
        safe_body = redaction.text if redaction else ticket.body
        injection = scan_for_injection(ticket.body)

        if redaction and redaction.redacted_any:
            run.add_step(
                "redact",
                f"Redacted {sum(redaction.counts.values())} PII value(s) before any model call",
                {"counts": redaction.counts},
            )
        if injection.detected:
            run.add_step(
                "safety",
                f"Possible prompt-injection signals: {', '.join(injection.signals)}",
                {"signals": injection.signals, "score": injection.score},
            )

        # -- 1. triage ---------------------------------------------------
        try:
            triage = await self._triage(run, safe_body)
        except StructuredOutputError as exc:
            return self._escalate(run, f"Triage failed to produce a valid classification: {exc}")

        if injection.should_escalate:
            return self._escalate(
                run,
                "The message contains a likely prompt-injection attempt. "
                "Routed to a human without taking any action.",
                triage=triage,
            )

        if self._over_budget(run):
            return self._budget_stop(run, triage)

        # -- 2. retrieve -------------------------------------------------
        await self._retrieve(run, triage, safe_body)

        # -- 3. context: load the order before planning ------------------
        context = ToolContext(
            commerce=self.commerce, ticket_id=ticket.id, correlation_id=ticket.external_id
        )
        order_context = await self._load_order(run, triage, context)

        # -- 4. plan -----------------------------------------------------
        try:
            plan = await self._plan(run, triage, safe_body, order_context)
        except StructuredOutputError as exc:
            return self._escalate(run, f"Planning failed: {exc}", triage=triage)

        if plan.needs_human:
            return self._escalate(
                run,
                plan.human_reason or "The agent determined a human is required.",
                triage=triage,
            )

        # -- 5. act ------------------------------------------------------
        # Restore real values in tool arguments before anything executes. The
        # model reasons over redacted text, but a tool must receive the real
        # postcode — queueing an approval containing "[POSTCODE_1]" would have
        # a human approve an action that then does the wrong thing.
        if redaction and redaction.redacted_any:
            plan = plan.model_copy(
                update={
                    "actions": [
                        action.model_copy(
                            update={
                                "arguments": {
                                    key: (
                                        redaction.restore(value)
                                        if isinstance(value, str)
                                        else value
                                    )
                                    for key, value in action.arguments.items()
                                }
                            }
                        )
                        for action in plan.actions
                    ]
                }
            )

        await self._act(run, plan, triage, injection_detected=injection.detected, context=context)

        if self._over_budget(run):
            return self._budget_stop(run, triage)

        # -- 6. draft ----------------------------------------------------
        try:
            reply = await self._draft(run, triage, safe_body)
        except StructuredOutputError as exc:
            return self._escalate(run, f"Reply drafting failed: {exc}", triage=triage)

        if redaction:
            # Real values go back in *after* the model is finished with the text.
            reply = reply.model_copy(update={"body": redaction.restore(reply.body)})

        # -- 7. verify ---------------------------------------------------
        verification = await self._verify(run, reply)

        return self._finalise(run, triage, reply, verification)

    # ------------------------------------------------------------------
    # stages
    # ------------------------------------------------------------------

    async def _triage(self, run: _Run, body: str) -> TriageResult:
        started = time.perf_counter()
        system, user = triage_prompt(run.ticket.subject, body)
        triage, usage = await self.llm.structured(
            TriageResult,
            task="triage",
            system=system,
            user=user,
            tier=ModelTier.FAST,
        )
        run.add_step(
            "triage",
            f"Classified as {triage.intent.value} ({triage.urgency.value}, "
            f"confidence {triage.confidence:.2f})",
            {
                "intent": triage.intent.value,
                "urgency": triage.urgency.value,
                "confidence": triage.confidence,
                "order_ref": triage.order_ref,
                "summary": triage.summary,
            },
            started=started,
            cost=usage.cost_usd,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )
        run.ticket.intent = triage.intent
        run.ticket.urgency = triage.urgency
        run.ticket.order_ref = triage.order_ref
        return triage

    async def _retrieve(self, run: _Run, triage: TriageResult, body: str) -> None:
        started = time.perf_counter()
        query = f"{triage.intent.value.replace('_', ' ')} {triage.summary} {body[:400]}"
        chunks = await self.retriever.retrieve(query, limit=self.settings.max_retrieval_chunks)
        run.chunks = chunks
        run.add_step(
            "retrieve",
            f"Retrieved {len(chunks)} policy passage(s)",
            {
                "query": query[:200],
                "results": [
                    {
                        "chunk_id": c.chunk.id,
                        "title": c.chunk.title,
                        "score": c.score,
                        "semantic": c.semantic_score,
                        "lexical": c.lexical_score,
                    }
                    for c in chunks
                ],
            },
            started=started,
        )

    async def _load_order(self, run: _Run, triage: TriageResult, context: ToolContext) -> str:
        if not triage.order_ref:
            return ""
        started = time.perf_counter()
        result = await self.registry.execute(
            _CONTEXT_TOOL, {"order_ref": triage.order_ref}, context
        )
        run.tool_results.append(result)
        if result.ok:
            run.facts.update(result.data)
            run.add_step(
                "act",
                f"Loaded order {triage.order_ref} ({result.data.get('status')})",
                {"tool": _CONTEXT_TOOL, "ok": True, "data": result.data},
                started=started,
            )
            return dumps(result.data)
        run.add_step(
            "act",
            f"Could not load order {triage.order_ref}: {result.error}",
            {"tool": _CONTEXT_TOOL, "ok": False, "error": result.error},
            started=started,
        )
        return f"Order lookup failed: {result.error}"

    async def _plan(
        self, run: _Run, triage: TriageResult, body: str, order_context: str
    ) -> AgentPlan:
        started = time.perf_counter()
        system, user = plan_prompt(
            intent=triage.intent.value,
            summary=triage.summary,
            order_ref=triage.order_ref,
            body=body,
            tool_catalogue=self.registry.catalogue(),
            order_context=order_context,
        )
        plan, usage = await self.llm.structured(
            AgentPlan,
            task="plan",
            system=system,
            user=user,
            tier=ModelTier.REASONING,
            metadata={
                "intent": triage.intent.value,
                "order_ref": triage.order_ref,
                "confidence": triage.confidence,
                "order_total_gbp": run.facts.get("total_gbp", 0.0),
            },
        )
        # The context tool has already run; never run it twice.
        actions = [a for a in plan.actions if a.tool != _CONTEXT_TOOL]
        actions = actions[: self.settings.max_tool_calls]
        plan = plan.model_copy(update={"actions": actions})

        run.add_step(
            "plan",
            f"Planned {len(plan.actions)} action(s)"
            + (" — needs human" if plan.needs_human else ""),
            {
                "actions": [a.model_dump() for a in plan.actions],
                "reasoning": plan.reasoning,
                "needs_human": plan.needs_human,
                "human_reason": plan.human_reason,
            },
            started=started,
            cost=usage.cost_usd,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )
        return plan

    async def _act(
        self,
        run: _Run,
        plan: AgentPlan,
        triage: TriageResult,
        *,
        injection_detected: bool,
        context: ToolContext,
    ) -> None:
        for action in plan.actions:
            started = time.perf_counter()
            verdict = self.policy.evaluate(
                action.tool,
                action.arguments,
                confidence=triage.confidence,
                injection_detected=injection_detected,
            )

            if verdict.outcome is PolicyOutcome.DENY:
                run.add_step(
                    "act",
                    f"Denied {action.tool}: {verdict.reason}",
                    {"tool": action.tool, "outcome": "deny", "reason": verdict.reason},
                    started=started,
                )
                continue

            if verdict.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                tool = self.registry.get(action.tool)
                summary = action.rationale
                if tool and tool.approval_summary:
                    # A prettier label for the human reviewing this. If it
                    # cannot be produced, the rationale is still shown — never
                    # fail an approval request over its own label.
                    with contextlib.suppress(Exception):
                        summary = tool.approval_summary(tool.parse_args(action.arguments))
                request = ApprovalRequest(
                    ticket_id=run.ticket.id,
                    tool=action.tool,
                    arguments=action.arguments,
                    rationale=summary,
                    risk=verdict.risk or ActionRisk.REQUIRES_APPROVAL,
                )
                run.approvals.append(request)
                run.add_step(
                    "approval",
                    f"Queued for human approval: {summary}",
                    {
                        "tool": action.tool,
                        "arguments": action.arguments,
                        "reason": verdict.reason,
                        "approval_id": request.id,
                        # Carried through so the approval row records the risk
                        # tier that caused the gate, not just that a gate fired.
                        "risk": request.risk.value,
                        "rationale": action.rationale,
                        # The bare human-readable summary, without the
                        # "Queued for human approval:" prefix the step carries.
                        # Stored separately so the audit trail does not end up
                        # reading "Rejected by X: Queued for human approval: ...".
                        "summary": summary,
                    },
                    started=started,
                )
                continue

            result = await self.registry.execute(action.tool, action.arguments, context)
            run.tool_results.append(result)
            if result.ok:
                run.facts.update(result.data)
            run.add_step(
                "act",
                (f"Ran {action.tool}" if result.ok else f"{action.tool} failed: {result.error}"),
                {
                    "tool": action.tool,
                    "ok": result.ok,
                    "arguments": action.arguments,
                    "data": result.data,
                    "error": result.error,
                    "policy": verdict.reason,
                },
                started=started,
            )

    async def _draft(self, run: _Run, triage: TriageResult, body: str) -> DraftedReply:
        started = time.perf_counter()
        system, user = draft_reply_prompt(
            customer_name=str(run.facts.get("customer_name", "")),
            subject=run.ticket.subject,
            intent=triage.intent.value,
            body=body,
            facts=dumps(run.facts) if run.facts else "",
            policy_extracts="\n\n".join(
                f"[{c.chunk.id}] {c.chunk.title}\n{c.chunk.text}" for c in run.chunks[:3]
            ),
        )
        reply, usage = await self.llm.structured(
            DraftedReply,
            task="draft_reply",
            system=system,
            user=user,
            tier=ModelTier.REASONING,
            metadata={
                "intent": triage.intent.value,
                "order_ref": triage.order_ref,
                "subject": run.ticket.subject,
                "customer_name": run.facts.get("customer_name", ""),
                "facts": run.facts,
                "chunks": [
                    {
                        "document_id": c.chunk.document_id,
                        "chunk_id": c.chunk.id,
                        "title": c.chunk.title,
                        "text": c.chunk.text,
                    }
                    for c in run.chunks[:3]
                ],
            },
        )
        run.add_step(
            "draft",
            f"Drafted reply ({len(reply.body.split())} words, {len(reply.citations)} citation(s))",
            {"subject": reply.subject, "citations": [c.chunk_id for c in reply.citations]},
            started=started,
            cost=usage.cost_usd,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )
        return reply

    async def _verify(self, run: _Run, reply: DraftedReply) -> VerificationResult:
        started = time.perf_counter()
        system, user = verify_prompt(
            reply_body=reply.body,
            facts=dumps(run.facts) if run.facts else "",
            policy_extracts="\n\n".join(f"[{c.chunk.id}] {c.chunk.text}" for c in run.chunks[:3]),
        )
        usage: Usage | None = None
        try:
            verification, usage = await self.llm.structured(
                VerificationResult,
                task="verify",
                system=system,
                user=user,
                tier=ModelTier.REASONING,
                metadata={
                    "reply_body": reply.body,
                    "facts": run.facts,
                    "citations": [c.model_dump() for c in reply.citations],
                },
            )
        except StructuredOutputError as exc:
            # A verifier that cannot answer is treated as a failed
            # verification, never as a pass.
            verification = VerificationResult(
                grounded=False,
                hallucination_risk=1.0,
                verdict=f"Verification could not complete: {exc}",
            )
            usage = None

        run.add_step(
            "verify",
            (
                "Reply verified as grounded"
                if verification.grounded
                else f"Verification failed: {verification.verdict}"
            ),
            verification.model_dump(),
            started=started,
            cost=usage.cost_usd if usage else 0.0,
            tokens_in=usage.input_tokens if usage else 0,
            tokens_out=usage.output_tokens if usage else 0,
        )
        return verification

    # ------------------------------------------------------------------
    # outcomes
    # ------------------------------------------------------------------

    def _over_budget(self, run: _Run) -> bool:
        return run.cost_usd > self.budget_usd

    def _base_outcome(self, run: _Run, triage: TriageResult | None) -> AgentOutcome:
        return AgentOutcome(
            ticket_id=run.ticket.id,
            status=TicketStatus.ESCALATED,
            intent=triage.intent if triage else None,
            urgency=triage.urgency if triage else None,
            order_ref=triage.order_ref if triage else None,
            steps=run.steps,
            tool_results=run.tool_results,
            pending_approvals=[a.id for a in run.approvals],
            total_cost_usd=round(run.cost_usd, 8),
            total_tokens_in=run.tokens_in,
            total_tokens_out=run.tokens_out,
            duration_ms=round((time.perf_counter() - run.started) * 1000, 3),
        )

    def _escalate(self, run: _Run, reason: str, triage: TriageResult | None = None) -> AgentOutcome:
        run.add_step("escalate", reason)
        log.info("agent.escalated", ticket_id=run.ticket.id, reason=reason)
        outcome = self._base_outcome(run, triage)
        return outcome.model_copy(
            update={"status": TicketStatus.ESCALATED, "escalation_reason": reason}
        )

    def _budget_stop(self, run: _Run, triage: TriageResult | None) -> AgentOutcome:
        reason = (
            f"Per-ticket budget of ${self.budget_usd:.4f} reached "
            f"(spent ${run.cost_usd:.4f}). Handed to a human rather than "
            "continuing with reduced context."
        )
        run.add_step("escalate", reason, {"budget_usd": self.budget_usd, "spent": run.cost_usd})
        outcome = self._base_outcome(run, triage)
        return outcome.model_copy(
            update={
                "status": TicketStatus.ESCALATED,
                "escalation_reason": reason,
                "budget_exceeded": True,
            }
        )

    def _finalise(
        self,
        run: _Run,
        triage: TriageResult,
        reply: DraftedReply,
        verification: VerificationResult,
    ) -> AgentOutcome:
        outcome = self._base_outcome(run, triage)
        update: dict[str, Any] = {"reply": reply, "verification": verification}

        if run.approvals:
            update["status"] = TicketStatus.AWAITING_APPROVAL
            update["escalation_reason"] = (
                f"{len(run.approvals)} action(s) require human approval before the "
                "reply can be sent."
            )
        elif not verification.grounded:
            update["status"] = TicketStatus.ESCALATED
            update["escalation_reason"] = "Drafted reply failed groundedness verification: " + (
                verification.verdict or "unsupported claims present"
            )
        elif len(reply.citations) < self.settings.min_citations_for_auto_send:
            update["status"] = TicketStatus.ESCALATED
            update["escalation_reason"] = (
                f"Reply cites {len(reply.citations)} policy passage(s); "
                f"{self.settings.min_citations_for_auto_send} required for automatic sending."
            )
        elif triage.confidence < self.settings.min_confidence_for_auto_action:
            update["status"] = TicketStatus.ESCALATED
            update["escalation_reason"] = (
                f"Classification confidence {triage.confidence:.2f} is below the "
                f"{self.settings.min_confidence_for_auto_action:.2f} automation threshold."
            )
        else:
            update["status"] = TicketStatus.RESOLVED

        log.info(
            "agent.finished",
            ticket_id=run.ticket.id,
            status=str(update["status"]),
            cost_usd=run.cost_usd,
            steps=len(run.steps),
        )
        return outcome.model_copy(update=update)
