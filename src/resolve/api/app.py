"""HTTP API.

Endpoints are deliberately few. The interesting surface of this system is the
agent and the workflow engine; the API's job is to accept work idempotently,
expose the audit trail, and let a human approve or reject the actions the
agent was not permitted to take alone.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from resolve import __version__
from resolve.api.schemas import (
    ActionOversight,
    AnalyticsResponse,
    ApprovalContext,
    ApprovalDecisionRequest,
    ApprovalView,
    AuditEvent,
    DecisionResultView,
    EvalReportResponse,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    OversightMetrics,
    RunView,
    StepView,
    TicketDetail,
    TicketView,
)
from resolve.api.security import require_api_key
from resolve.bus.base import Topics
from resolve.db.base import as_utc
from resolve.db.models import AgentRun, AgentStepRow, Approval, IdempotencyKey
from resolve.db.models import Ticket as TicketRow
from resolve.db.session import session_scope
from resolve.domain.enums import ApprovalDecision, ApprovalOutcome
from resolve.evaluation_store import (
    EvaluationStore,
    InvalidReportError,
    NoReportError,
)
from resolve.logging import get_logger, set_correlation_id
from resolve.oversight import (
    AlreadyDecidedError,
    ApprovalNotFoundError,
    HumanDecisionService,
)
from resolve.runtime import Runtime, build_runtime
from resolve.telemetry import APPROVALS_PENDING, LLM_CACHE_HIT_RATE

log = get_logger(__name__)

TView = TypeVar("TView", bound=BaseModel)

#: Assumptions behind the projected-hours-saved figure. Published with the
#: number so nobody has to take it on trust. See CASE_STUDY.md.
BUSINESS_ASSUMPTIONS = {
    "minutes_per_ticket_manual": 6.0,
    "minutes_per_ticket_reviewed": 1.5,
}


def _project(row: object, view: type[TView], **overrides: Any) -> TView:
    """Build a response model from an ORM row by field name.

    Deliberately not a hand-written list of columns. The previous version was
    one, and when `reply_state` was added to both the table and the view it was
    silently dropped in between — the API served the field's default and the
    console showed "none" for every reply. Driving the projection from the
    view's own fields makes that failure impossible.
    """
    data = {
        name: getattr(row, name)
        for name in view.model_fields
        if name not in overrides and hasattr(row, name)
    }
    return view(**data, **overrides)


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only if lifespan did not run
        raise HTTPException(status_code=503, detail="runtime not ready")
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.runtime = await build_runtime()
    try:
        yield
    finally:
        await app.state.runtime.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Resolve",
        version=__version__,
        description=(
            "Autonomous order-operations agent. Triages inbound customer messages, "
            "grounds itself in order data and written policy, and either resolves "
            "the request or escalates it — under budget, safety and approval "
            "constraints."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        set_correlation_id(correlation_id)
        response: Response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        return response

    # ---------------------------------------------------------------- health

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    async def healthz(runtime: Runtime = Depends(get_runtime)) -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=__version__,
            provider=runtime.settings.llm.provider,
            environment=runtime.settings.environment.value,
        )

    @app.get("/readyz", response_model=HealthResponse, tags=["ops"])
    async def readyz(runtime: Runtime = Depends(get_runtime)) -> HealthResponse:
        """Readiness reports *which* dependency is unhealthy.

        The audited original crashed at import time when an unrelated
        integration's credentials were missing, taking the whole payment queue
        down. Here a degraded dependency is reported, not fatal.
        """
        checks: dict[str, str] = {}
        try:
            async with session_scope(runtime.session_factory) as session:
                await session.execute(select(func.count()).select_from(TicketRow))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
        try:
            await runtime.bus.pending_count(Topics.TICKET_RECEIVED)
            checks["bus"] = "ok"
        except Exception as exc:
            checks["bus"] = f"error: {type(exc).__name__}"
        checks["llm_provider"] = runtime.settings.llm.provider
        checks["retriever"] = f"{runtime.application.policy_chunks} chunks indexed"

        overall = (
            "ok"
            if all(v == "ok" or not v.startswith("error") for v in checks.values())
            else "degraded"
        )
        return HealthResponse(
            status=overall,
            version=__version__,
            provider=runtime.settings.llm.provider,
            environment=runtime.settings.environment.value,
            checks=checks,
        )

    @app.get("/metrics", tags=["ops"])
    async def metrics(runtime: Runtime = Depends(get_runtime)) -> Response:
        cache = runtime.application.llm.cache
        if cache is not None:
            LLM_CACHE_HIT_RATE.set(cache.hit_rate)
        async with session_scope(runtime.session_factory) as session:
            pending = (
                await session.execute(
                    select(func.count()).select_from(Approval).where(Approval.decision == "pending")
                )
            ).scalar_one()
        APPROVALS_PENDING.set(pending)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # --------------------------------------------------------------- ingest

    @app.post(
        "/v1/tickets",
        response_model=IngestResponse,
        status_code=202,
        tags=["tickets"],
        dependencies=[Depends(require_api_key)],
    )
    async def ingest(
        payload: IngestRequest,
        runtime: Runtime = Depends(get_runtime),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> IngestResponse:
        """Accept an inbound customer message.

        Idempotent twice over: by `Idempotency-Key` when the caller supplies
        one, and by `external_id` regardless. A webhook that fires three times
        creates exactly one ticket.
        """
        async with session_scope(runtime.session_factory) as session:
            if idempotency_key:
                seen = await session.get(IdempotencyKey, idempotency_key)
                if seen is not None:
                    return IngestResponse.model_validate({**seen.response, "duplicate": True})

            existing = (
                await session.execute(
                    select(TicketRow).where(TicketRow.external_id == payload.external_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return IngestResponse(ticket_id=existing.id, status=existing.status, duplicate=True)

            ticket = TicketRow(
                external_id=payload.external_id,
                channel=payload.channel,
                from_email=payload.from_email,
                subject=payload.subject,
                body=payload.body,
            )
            session.add(ticket)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                found = (
                    await session.execute(
                        select(TicketRow).where(TicketRow.external_id == payload.external_id)
                    )
                ).scalar_one()
                return IngestResponse(ticket_id=found.id, status=found.status, duplicate=True)

            response = IngestResponse(ticket_id=ticket.id, status=ticket.status)
            if idempotency_key:
                session.add(
                    IdempotencyKey(
                        key=idempotency_key,
                        endpoint="POST /v1/tickets",
                        response=response.model_dump(),
                        status_code=202,
                    )
                )

        await runtime.bus.publish(
            Topics.TICKET_RECEIVED,
            {"ticket_id": response.ticket_id, "external_id": payload.external_id},
            correlation_id=payload.external_id,
        )
        log.info("ticket.ingested", ticket_id=response.ticket_id)
        return response

    # -------------------------------------------------------------- tickets

    @app.get(
        "/v1/tickets",
        response_model=list[TicketView],
        tags=["tickets"],
        dependencies=[Depends(require_api_key)],
    )
    async def list_tickets(
        status_filter: str | None = None,
        limit: int = 50,
        runtime: Runtime = Depends(get_runtime),
    ) -> list[TicketView]:
        async with session_scope(runtime.session_factory) as session:
            query = select(TicketRow).order_by(TicketRow.created_at.desc()).limit(min(limit, 200))
            if status_filter:
                query = query.where(TicketRow.status == status_filter)
            rows = (await session.execute(query)).scalars().all()
            return [TicketView.model_validate(r, from_attributes=True) for r in rows]

    @app.get(
        "/v1/tickets/{ticket_id}",
        response_model=TicketDetail,
        tags=["tickets"],
        dependencies=[Depends(require_api_key)],
    )
    async def get_ticket(ticket_id: str, runtime: Runtime = Depends(get_runtime)) -> TicketDetail:
        """Full audit trail: the ticket, every agent run, and every step."""
        async with session_scope(runtime.session_factory) as session:
            ticket = await session.get(TicketRow, ticket_id)
            if ticket is None:
                raise HTTPException(status_code=404, detail="ticket not found")

            runs = (
                (
                    await session.execute(
                        select(AgentRun)
                        .where(AgentRun.ticket_id == ticket_id)
                        .order_by(AgentRun.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            views: list[RunView] = []
            for run in runs:
                steps = (
                    (
                        await session.execute(
                            select(AgentStepRow)
                            .where(AgentStepRow.run_id == run.id)
                            .order_by(AgentStepRow.index)
                        )
                    )
                    .scalars()
                    .all()
                )
                views.append(
                    _project(
                        run,
                        RunView,
                        steps=[
                            _project(s, StepView, created_at=as_utc(s.created_at)) for s in steps
                        ],
                    )
                )

            return _project(ticket, TicketDetail, runs=views)

    # ------------------------------------------------------------ approvals

    @app.get(
        "/v1/approvals",
        response_model=list[ApprovalView],
        tags=["approvals"],
        dependencies=[Depends(require_api_key)],
    )
    async def list_approvals(
        decision: str = "pending", runtime: Runtime = Depends(get_runtime)
    ) -> list[ApprovalView]:
        async with session_scope(runtime.session_factory) as session:
            rows = (
                (
                    await session.execute(
                        select(Approval)
                        .where(Approval.decision == decision)
                        .order_by(Approval.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [ApprovalView.model_validate(r, from_attributes=True) for r in rows]

    @app.get(
        "/v1/approvals/{approval_id}",
        response_model=ApprovalContext,
        tags=["approvals"],
        dependencies=[Depends(require_api_key)],
    )
    async def approval_context(
        approval_id: str, runtime: Runtime = Depends(get_runtime)
    ) -> ApprovalContext:
        """An approval with the context needed to judge it.

        The customer's actual words, the agent's rationale, the policy it
        retrieved, and the reply that will be sent if this is approved — or
        voided if it is not. A reviewer shown only the arguments is being asked
        to rubber-stamp something they cannot evaluate.
        """
        async with session_scope(runtime.session_factory) as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                raise HTTPException(status_code=404, detail="approval not found")
            ticket = await session.get(TicketRow, approval.ticket_id)
            run = await session.get(AgentRun, approval.run_id) if approval.run_id else None

            citations: list[dict[str, str]] = []
            ai_rationale = approval.rationale
            if run is not None:
                steps = (
                    (
                        await session.execute(
                            select(AgentStepRow)
                            .where(AgentStepRow.run_id == run.id)
                            .order_by(AgentStepRow.index)
                        )
                    )
                    .scalars()
                    .all()
                )
                for step in steps:
                    if step.kind == "retrieve":
                        for r in (step.detail.get("results") or [])[:3]:
                            citations.append(
                                {
                                    "chunk_id": str(r.get("chunk_id", "")),
                                    "title": str(r.get("title", "")),
                                    "score": f"{float(r.get('score', 0)):.4f}",
                                }
                            )
                    elif step.kind == "plan":
                        for action in step.detail.get("actions") or []:
                            if action.get("tool") == approval.tool:
                                ai_rationale = str(action.get("rationale") or ai_rationale)

            return ApprovalContext(
                approval=ApprovalView.model_validate(approval, from_attributes=True),
                ticket_subject=ticket.subject if ticket else "",
                customer_email=ticket.from_email if ticket else "",
                customer_message=ticket.body if ticket else "",
                intent=ticket.intent if ticket else None,
                ai_rationale=ai_rationale,
                policy_citations=citations,
                proposed_reply_subject=run.reply_subject if run else None,
                proposed_reply_body=run.reply_body if run else None,
                reply_state=run.reply_state if run else "none",
            )

    @app.post(
        "/v1/approvals/{approval_id}",
        response_model=DecisionResultView,
        tags=["approvals"],
        responses={409: {"description": "This approval was already decided"}},
    )
    async def decide_approval(
        approval_id: str,
        decision: ApprovalDecisionRequest,
        api_key: str = Depends(require_api_key),
        runtime: Runtime = Depends(get_runtime),
    ) -> DecisionResultView:
        """Record a human decision and everything that follows from it.

        The whole lifecycle lives in `resolve.oversight`, not here: a decision
        changes the approval, the run, the drafted reply, the ticket and the
        trace, and those consequences are domain logic. This route validates,
        delegates, and reports.

        Deciding twice is a 409, always. That guard is what stops a double-click
        from issuing two refunds.
        """
        service = HumanDecisionService(runtime.application.registry, runtime.application.commerce)
        async with session_scope(runtime.session_factory) as session:
            try:
                result = await service.decide(
                    session,
                    approval_id,
                    approve=decision.approve,
                    decided_by=decision.decided_by,
                    note=decision.note,
                )
            except ApprovalNotFoundError as exc:
                raise HTTPException(status_code=404, detail="approval not found") from exc
            except AlreadyDecidedError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            run = (
                await session.get(AgentRun, result.approval.run_id)
                if result.approval.run_id
                else None
            )
            view = DecisionResultView(
                approval=ApprovalView.model_validate(result.approval, from_attributes=True),
                ticket_id=result.approval.ticket_id,
                ticket_status=result.ticket_status.value,
                reply_state=result.reply_state.value,
                reply_state_reason=run.reply_state_reason if run else None,
                escalation_reason=result.escalation_reason,
                executed=result.executed,
                outcome=result.outcome.value,
            )

        await runtime.bus.publish(
            Topics.APPROVAL_DECIDED,
            {
                "approval_id": approval_id,
                "approved": decision.approve,
                "outcome": view.outcome,
                "ticket_status": view.ticket_status,
            },
            correlation_id=approval_id,
        )
        return view

    # ------------------------------------------------------------- oversight

    @app.get(
        "/v1/oversight",
        response_model=OversightMetrics,
        tags=["oversight"],
        dependencies=[Depends(require_api_key)],
    )
    async def oversight(runtime: Runtime = Depends(get_runtime)) -> OversightMetrics:
        """How humans are supervising the agent, on real traffic.

        Kept apart from the evaluation report on purpose. Evaluation asks
        whether the AI behaved correctly against a fixed golden set; this asks
        what operators actually did about the actions it proposed. A system can
        score perfectly on the former while operators reject most of the
        latter, and merging the two would hide exactly that.

        Every figure is counted from decided approvals. Nothing is projected.
        """
        async with session_scope(runtime.session_factory) as session:
            approvals = (await session.execute(select(Approval))).scalars().all()

        pending = [a for a in approvals if a.decision == ApprovalDecision.PENDING.value]
        approved = [a for a in approvals if a.decision == ApprovalDecision.APPROVED.value]
        rejected = [a for a in approvals if a.decision == ApprovalDecision.REJECTED.value]
        failed = [a for a in approvals if a.outcome == ApprovalOutcome.EXECUTION_FAILED.value]
        decided = len(approved) + len(rejected)

        by_action: dict[str, dict[str, int]] = {}
        for a in approvals:
            bucket = by_action.setdefault(
                a.tool, {"proposed": 0, "approved": 0, "rejected": 0, "pending": 0}
            )
            bucket["proposed"] += 1
            if a.decision == ApprovalDecision.APPROVED.value:
                bucket["approved"] += 1
            elif a.decision == ApprovalDecision.REJECTED.value:
                bucket["rejected"] += 1
            else:
                bucket["pending"] += 1

        durations: list[float] = []
        for a in approvals:
            decided_at = as_utc(a.decided_at)
            requested_at = as_utc(a.created_at)
            if decided_at is not None and requested_at is not None:
                durations.append((decided_at - requested_at).total_seconds())
        durations.sort()
        # None, not 0.0, when nothing has been decided. A zero here would read
        # as "decided instantly", which is a different and false claim.
        median = round(durations[len(durations) // 2], 2) if durations else None

        return OversightMetrics(
            decisions_total=len(approvals),
            pending=len(pending),
            approved=len(approved),
            rejected=len(rejected),
            execution_failed=len(failed),
            approval_rate=round(len(approved) / decided, 4) if decided else 0.0,
            rejection_rate=round(len(rejected) / decided, 4) if decided else 0.0,
            override_rate=round(len(rejected) / decided, 4) if decided else 0.0,
            by_action=[
                ActionOversight(
                    action=action,
                    proposed=b["proposed"],
                    approved=b["approved"],
                    rejected=b["rejected"],
                    pending=b["pending"],
                    approval_rate=(
                        round(b["approved"] / (b["approved"] + b["rejected"]), 4)
                        if (b["approved"] + b["rejected"])
                        else 0.0
                    ),
                )
                for action, b in sorted(by_action.items())
            ],
            total_value_approved_gbp=round(sum(a.amount_gbp or 0.0 for a in approved), 2),
            total_value_rejected_gbp=round(sum(a.amount_gbp or 0.0 for a in rejected), 2),
            median_time_to_decision_seconds=median,
        )

    @app.get(
        "/v1/audit",
        response_model=list[AuditEvent],
        tags=["oversight"],
        dependencies=[Depends(require_api_key)],
    )
    async def audit_log(
        limit: int = 100, runtime: Runtime = Depends(get_runtime)
    ) -> list[AuditEvent]:
        """Every human decision, newest first.

        The durable record of who authorised what. Read-only by construction:
        there is no endpoint that edits or deletes an entry.
        """
        async with session_scope(runtime.session_factory) as session:
            rows = (
                (
                    await session.execute(
                        select(Approval)
                        .where(Approval.decided_at.is_not(None))
                        .order_by(Approval.decided_at.desc())
                        .limit(min(limit, 500))
                    )
                )
                .scalars()
                .all()
            )
            subjects: dict[str, str] = {}
            for row in rows:
                if row.ticket_id not in subjects:
                    ticket = await session.get(TicketRow, row.ticket_id)
                    subjects[row.ticket_id] = ticket.subject if ticket else ""

            return [
                AuditEvent(
                    approval_id=r.id,
                    ticket_id=r.ticket_id,
                    action=r.tool,
                    resource=r.resource,
                    amount_gbp=r.amount_gbp,
                    risk=r.risk,
                    decision=r.decision,
                    outcome=r.outcome,
                    actor=r.decided_by,
                    actor_type=r.decided_by_type,
                    note=r.note,
                    decided_at=as_utc(r.decided_at) or r.created_at,
                    ticket_subject=subjects.get(r.ticket_id, ""),
                )
                for r in rows
            ]

    # ------------------------------------------------------------------ evals

    @app.get(
        "/v1/evals/latest",
        response_model=EvalReportResponse,
        tags=["evaluation"],
        dependencies=[Depends(require_api_key)],
        responses={
            404: {"description": "No evaluation has been run yet"},
            500: {"description": "A report exists but does not match the schema"},
        },
    )
    async def latest_evaluation(
        provider: str | None = None,
        runtime: Runtime = Depends(get_runtime),
    ) -> EvalReportResponse:
        """Serve the most recent evaluation report.

        The console reads this endpoint rather than the filesystem. It used to
        read the file directly, which worked in a dev checkout and silently
        failed in Docker, because the console image does not contain `evals/`.

        Three outcomes, all honest:

        * a valid report exists  → 200 with the report and its provenance;
        * no report exists       → 404, so the console shows an empty state
          telling the operator to run `make eval`;
        * a report exists but is malformed → 500, reported as a problem rather
          than parsed into something that merely looks like results.

        Nothing here computes, defaults or infers a metric. Every number
        returned was produced by a run of the harness.
        """
        store = EvaluationStore(runtime.settings.evals.results_dir)
        try:
            report, path = store.latest(provider)
        except NoReportError as exc:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No evaluation report has been generated yet. Run `make eval` to produce one."
                ),
            ) from exc
        except InvalidReportError as exc:
            log.error("evals.invalid_report", path=str(exc.path), detail=exc.detail)
            raise HTTPException(
                status_code=500,
                detail=f"The evaluation report is not valid: {exc.detail}",
            ) from exc

        return EvalReportResponse(
            report=report,
            source=path.name,
            available_providers=store.available_providers(),
        )

    # ------------------------------------------------------------ analytics

    @app.get(
        "/v1/analytics",
        response_model=AnalyticsResponse,
        tags=["analytics"],
        dependencies=[Depends(require_api_key)],
    )
    async def analytics(runtime: Runtime = Depends(get_runtime)) -> AnalyticsResponse:
        async with session_scope(runtime.session_factory) as session:
            total = (
                await session.execute(select(func.count()).select_from(TicketRow))
            ).scalar_one()
            status_rows = (
                await session.execute(
                    select(TicketRow.status, func.count()).group_by(TicketRow.status)
                )
            ).all()
            by_status: dict[str, int] = {str(row[0]): int(row[1]) for row in status_rows}
            intent_rows = (
                await session.execute(
                    select(TicketRow.intent, func.count())
                    .where(TicketRow.intent.is_not(None))
                    .group_by(TicketRow.intent)
                )
            ).all()
            by_intent: dict[str, int] = {str(row[0]): int(row[1]) for row in intent_rows}
            agg = (
                await session.execute(
                    select(
                        func.count(AgentRun.id),
                        func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
                        func.coalesce(func.avg(AgentRun.duration_ms), 0.0),
                        func.count(AgentRun.id).filter(AgentRun.grounded.is_(True)),
                        func.count(AgentRun.id).filter(AgentRun.grounded.is_not(None)),
                    )
                )
            ).one()
            pending = (
                await session.execute(
                    select(func.count()).select_from(Approval).where(Approval.decision == "pending")
                )
            ).scalar_one()

        run_count, total_cost, mean_duration, grounded_ok, grounded_total = agg
        resolved = int(by_status.get("resolved", 0))
        awaiting = int(by_status.get("awaiting_approval", 0))
        escalated = int(by_status.get("escalated", 0))
        denom = max(total, 1)

        manual = BUSINESS_ASSUMPTIONS["minutes_per_ticket_manual"]
        reviewed = BUSINESS_ASSUMPTIONS["minutes_per_ticket_reviewed"]
        # Fully autonomous tickets save the whole handling time; approved ones
        # save the difference between writing a reply and reviewing one.
        minutes_saved = resolved * manual + awaiting * (manual - reviewed)

        return AnalyticsResponse(
            tickets_total=total,
            by_status=by_status,
            by_intent=by_intent,
            auto_resolution_rate=round(resolved / denom, 4),
            approval_rate=round(awaiting / denom, 4),
            escalation_rate=round(escalated / denom, 4),
            groundedness_rate=round(grounded_ok / grounded_total, 4) if grounded_total else 0.0,
            total_cost_usd=round(float(total_cost), 6),
            mean_cost_usd=round(float(total_cost) / run_count, 6) if run_count else 0.0,
            mean_duration_ms=round(float(mean_duration), 2),
            pending_approvals=int(pending),
            projected_hours_saved=round(minutes_saved / 60, 2),
            assumptions=BUSINESS_ASSUMPTIONS,
        )

    return app


app = create_app()
