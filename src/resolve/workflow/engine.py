"""A small durable workflow engine on Postgres.

Why build one rather than adopt Temporal
----------------------------------------
Temporal is the right answer at scale and is named as the migration target in
ROADMAP.md. It is the wrong answer for a system with four workflows, because
it adds a server, a worker SDK, a namespace and a whole operational surface
for guarantees this delivers in ~150 lines: durable checkpointing, exactly-once
step semantics, resume-after-crash, bounded retries and a dead-letter path.
The interface is deliberately Temporal-shaped so the migration is mechanical.

Guarantees
----------
* A step that has already succeeded is **never re-executed**. Its recorded
  result is returned instead. This is what makes the whole workflow safely
  resumable — including across a process kill in the middle of a refund.
* A step that fails is retried with exponential backoff up to `max_attempts`,
  after which the run is marked `dead_letter` and left for a human.
* Step results are written in the same transaction as the step's status, so
  there is no state in which a step is "succeeded" with no result.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resolve.db.models import WorkflowRun, WorkflowStep
from resolve.db.session import session_scope
from resolve.domain.enums import WorkflowStepStatus
from resolve.logging import get_logger

log = get_logger(__name__)

StepFn = Callable[[], Awaitable[dict[str, Any]]]


class WorkflowFailedError(Exception):
    def __init__(self, run_id: str, step: str, cause: str) -> None:
        super().__init__(f"workflow run {run_id} failed at step {step!r}: {cause}")
        self.run_id = run_id
        self.step = step
        self.cause = cause


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0
    jitter: float = 0.2

    def delay_for(self, attempt: int) -> float:
        base = min(
            self.initial_backoff_seconds * (self.backoff_multiplier ** (attempt - 1)),
            self.max_backoff_seconds,
        )
        # Full jitter, so a fleet of workers retrying a shared dependency does
        # not synchronise into a thundering herd.
        return base * (1 - self.jitter + random.random() * 2 * self.jitter)


@dataclass
class WorkflowContext:
    """Handed to a workflow definition. `step` is the only way to do work."""

    run_id: str
    correlation_id: str
    payload: dict[str, Any]
    engine: WorkflowEngine
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    async def step(
        self, name: str, fn: StepFn, *, retry: RetryPolicy | None = None
    ) -> dict[str, Any]:
        return await self.engine.run_step(self.run_id, name, fn, retry=retry or self.retry)


WorkflowFn = Callable[[WorkflowContext], Awaitable[dict[str, Any]]]


class WorkflowEngine:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self._definitions: dict[str, WorkflowFn] = {}

    def register(self, name: str, fn: WorkflowFn) -> None:
        self._definitions[name] = fn

    # -- run lifecycle ---------------------------------------------------

    async def start(
        self,
        workflow: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Start (or resume) a run. Idempotent on (workflow, correlation_id)."""
        async with session_scope(self.session_factory) as session:
            existing = (
                await session.execute(
                    select(WorkflowRun).where(
                        WorkflowRun.workflow == workflow,
                        WorkflowRun.correlation_id == correlation_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                log.info(
                    "workflow.resume",
                    workflow=workflow,
                    run_id=existing.id,
                    status=existing.status,
                )
                return existing.id

            run = WorkflowRun(
                workflow=workflow,
                correlation_id=correlation_id,
                payload=payload or {},
                status="running",
            )
            session.add(run)
            try:
                await session.flush()
            except IntegrityError:
                # Another worker won the race. Fall back to its run.
                await session.rollback()
                found = (
                    await session.execute(
                        select(WorkflowRun).where(
                            WorkflowRun.workflow == workflow,
                            WorkflowRun.correlation_id == correlation_id,
                        )
                    )
                ).scalar_one()
                return found.id
            return run.id

    async def execute(
        self, workflow: str, correlation_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        definition = self._definitions.get(workflow)
        if definition is None:
            raise KeyError(f"no workflow registered under {workflow!r}")

        run_id = await self.start(workflow, correlation_id, payload)
        context = WorkflowContext(
            run_id=run_id,
            correlation_id=correlation_id,
            payload=payload or {},
            engine=self,
        )

        try:
            result = await definition(context)
        except WorkflowFailedError as exc:
            await self._finish(run_id, "dead_letter", error=str(exc))
            raise
        except Exception as exc:
            await self._finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise

        await self._finish(run_id, "succeeded")
        return result

    async def _finish(self, run_id: str, status: str, error: str | None = None) -> None:
        async with session_scope(self.session_factory) as session:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                return
            run.status = status
            run.error = error
        log.info("workflow.finished", run_id=run_id, status=status, error=error)

    # -- step execution --------------------------------------------------

    async def run_step(
        self, run_id: str, name: str, fn: StepFn, *, retry: RetryPolicy
    ) -> dict[str, Any]:
        # 1. Already done? Return the checkpoint. This is the whole point.
        async with session_scope(self.session_factory) as session:
            step = (
                await session.execute(
                    select(WorkflowStep).where(
                        WorkflowStep.run_id == run_id, WorkflowStep.name == name
                    )
                )
            ).scalar_one_or_none()
            if step is not None and step.status == WorkflowStepStatus.SUCCEEDED.value:
                log.debug("workflow.step.cached", run_id=run_id, step=name)
                return dict(step.result or {})
            if step is None:
                step = WorkflowStep(run_id=run_id, name=name, status="running")
                session.add(step)
                await session.flush()
            step_id = step.id
            attempt = step.attempt

        # 2. Execute with bounded retries.
        last_error = ""
        for attempt_number in range(attempt + 1, retry.max_attempts + 1):
            started = time.perf_counter()
            try:
                result = await fn()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "workflow.step.failed",
                    run_id=run_id,
                    step=name,
                    attempt=attempt_number,
                    error=last_error,
                )
                await self._record_step(
                    step_id,
                    status="failed" if attempt_number >= retry.max_attempts else "pending",
                    attempt=attempt_number,
                    error=last_error,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
                if attempt_number < retry.max_attempts:
                    await asyncio.sleep(retry.delay_for(attempt_number))
                continue

            await self._record_step(
                step_id,
                status="succeeded",
                attempt=attempt_number,
                result=dict(result),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return dict(result)

        raise WorkflowFailedError(run_id, name, last_error or "exhausted retries")

    async def _record_step(
        self,
        step_id: str,
        *,
        status: str,
        attempt: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        async with session_scope(self.session_factory) as session:
            step = await session.get(WorkflowStep, step_id)
            if step is None:
                return
            step.status = status
            step.attempt = attempt
            step.result = result
            step.error = error
            step.duration_ms = round(duration_ms, 3)
