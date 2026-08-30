"""Durability.

These are the tests that justify building a workflow engine at all: if a
completed step can re-execute, the whole design is worthless, because the step
that re-executes will eventually be a refund.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from resolve.db.models import WorkflowRun, WorkflowStep
from resolve.db.session import session_scope
from resolve.workflow.engine import RetryPolicy, WorkflowContext, WorkflowFailedError


class TestIdempotency:
    async def test_starting_twice_reuses_the_same_run(self, runtime) -> None:
        a = await runtime.workflows.start("demo", "corr-1", {"x": 1})
        b = await runtime.workflows.start("demo", "corr-1", {"x": 1})
        assert a == b

    async def test_completed_steps_are_never_re_executed(self, runtime) -> None:
        calls = {"n": 0}

        async def side_effect() -> dict[str, int]:
            calls["n"] += 1
            return {"n": calls["n"]}

        async def workflow(ctx: WorkflowContext) -> dict[str, int]:
            return await ctx.step("charge_card", side_effect)

        runtime.workflows.register("charge", workflow)

        first = await runtime.workflows.execute("charge", "order-1")
        second = await runtime.workflows.execute("charge", "order-1")

        assert calls["n"] == 1, "a completed step must not run twice"
        assert first == second == {"n": 1}

    async def test_resume_skips_completed_steps_only(self, runtime) -> None:
        executed: list[str] = []

        async def make(name: str, fail_once: bool = False):  # type: ignore[no-untyped-def]
            state = {"failed": False}

            async def step() -> dict[str, str]:
                if fail_once and not state["failed"]:
                    state["failed"] = True
                    raise RuntimeError("transient")
                executed.append(name)
                return {"step": name}

            return step

        step_a = await make("a")
        step_b = await make("b", fail_once=True)

        async def workflow(ctx: WorkflowContext) -> dict[str, str]:
            await ctx.step("a", step_a)
            return await ctx.step("b", step_b)

        runtime.workflows.register("resume", workflow)
        await runtime.workflows.execute("resume", "corr-resume")

        # 'a' ran once; 'b' failed once then succeeded, so it appears once too.
        assert executed == ["a", "b"]


class TestRetries:
    async def test_transient_failure_is_retried(self, runtime) -> None:
        attempts = {"n": 0}

        async def flaky() -> dict[str, int]:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("carrier API unavailable")
            return {"attempts": attempts["n"]}

        async def workflow(ctx: WorkflowContext) -> dict[str, int]:
            return await ctx.step("call_carrier", flaky)

        runtime.workflows.register("flaky", workflow)
        result = await runtime.workflows.execute("flaky", "corr-flaky")
        assert result == {"attempts": 3}

    async def test_permanent_failure_dead_letters(self, runtime) -> None:
        async def always_fails() -> dict[str, str]:
            raise ValueError("bad request")

        async def workflow(ctx: WorkflowContext) -> dict[str, str]:
            return await ctx.step(
                "doomed", always_fails, retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0)
            )

        runtime.workflows.register("doomed", workflow)
        with pytest.raises(WorkflowFailedError):
            await runtime.workflows.execute("doomed", "corr-doomed")

        async with session_scope(runtime.session_factory) as session:
            run = (
                await session.execute(
                    select(WorkflowRun).where(WorkflowRun.correlation_id == "corr-doomed")
                )
            ).scalar_one()
            assert run.status == "dead_letter"
            step = (
                await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == run.id))
            ).scalar_one()
            assert step.status == "failed"
            assert "bad request" in (step.error or "")

    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(
            initial_backoff_seconds=1, backoff_multiplier=2, max_backoff_seconds=10
        )
        assert policy.delay_for(1) < policy.delay_for(4)
        assert policy.delay_for(20) <= 10 * (1 + policy.jitter)
