"""Delivery semantics and crash recovery.

The audited original dequeued a payment job before the worker had done
anything with it, so a worker crash destroyed the job silently. These tests
assert the opposite property.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from resolve.bus.base import Topics
from resolve.bus.memory import InMemoryBus
from resolve.bus.streams import RedisStreamBus
from resolve.db.models import Ticket as TicketRow
from resolve.db.session import session_scope
from resolve.workers.agent_worker import AgentWorker


class TestBusSemantics:
    async def test_unacked_message_stays_pending(self) -> None:
        bus = InMemoryBus(claim_idle_seconds=0.0)
        await bus.publish("t", {"a": 1})
        events = await bus.consume("t", "worker-1")
        assert len(events) == 1
        assert await bus.pending_count("t") == 1

    async def test_ack_clears_pending(self) -> None:
        bus = InMemoryBus()
        await bus.publish("t", {"a": 1})
        (event,) = await bus.consume("t", "worker-1")
        await bus.ack("t", event.message_id)
        assert await bus.pending_count("t") == 0

    async def test_another_worker_reclaims_abandoned_work(self) -> None:
        """A worker that dies mid-job must not take the job with it."""
        bus = InMemoryBus(claim_idle_seconds=0.0)
        await bus.publish("t", {"a": 1})
        await bus.consume("t", "worker-that-dies")

        reclaimed = await bus.claim_stale("t", "worker-2")
        assert len(reclaimed) == 1
        assert reclaimed[0].attempts == 1

    async def test_a_worker_does_not_reclaim_its_own_in_flight_work(self) -> None:
        bus = InMemoryBus(claim_idle_seconds=0.0)
        await bus.publish("t", {"a": 1})
        await bus.consume("t", "worker-1")
        assert await bus.claim_stale("t", "worker-1") == []


class TestAgentWorker:
    async def _seed_ticket(self, runtime, body: str) -> str:
        async with session_scope(runtime.session_factory) as session:
            ticket = TicketRow(
                external_id=f"E-{abs(hash(body)) % 10**6}",
                from_email="c@example.com",
                subject="Where is my order?",
                body=body,
            )
            session.add(ticket)
            await session.flush()
            return ticket.id

    async def test_processes_a_ticket_end_to_end(self, runtime) -> None:
        ticket_id = await self._seed_ticket(runtime, "Any update on ORD-400013?")
        await runtime.bus.publish(Topics.TICKET_RECEIVED, {"ticket_id": ticket_id})

        worker = AgentWorker(runtime, name="test-worker")
        assert await worker.run_once() == 1
        assert worker.processed == 1
        assert await runtime.bus.pending_count(Topics.TICKET_RECEIVED) == 0

        async with session_scope(runtime.session_factory) as session:
            ticket = await session.get(TicketRow, ticket_id)
            assert ticket is not None
            assert ticket.status == "resolved"
            assert ticket.intent == "order_status"

    async def test_redelivery_does_not_reprocess_the_agent(self, runtime) -> None:
        """The expensive step is checkpointed, so redelivery is cheap and safe."""
        ticket_id = await self._seed_ticket(runtime, "Update on ORD-400014 please?")
        await runtime.bus.publish(Topics.TICKET_RECEIVED, {"ticket_id": ticket_id})

        worker = AgentWorker(runtime, name="w1")
        await worker.run_once()
        calls_after_first = len(runtime.application.llm.provider.calls)

        # Simulate the same message being delivered again after a crash.
        await runtime.bus.publish(Topics.TICKET_RECEIVED, {"ticket_id": ticket_id})
        await AgentWorker(runtime, name="w2").run_once()

        assert len(runtime.application.llm.provider.calls) == calls_after_first

    async def test_malformed_event_is_survived(self, runtime) -> None:
        await runtime.bus.publish(Topics.TICKET_RECEIVED, {"nonsense": True})
        worker = AgentWorker(runtime, name="w")
        assert await worker.run_once() == 1
        assert worker.processed == 0


class TestTicketProjection:
    """What the worker writes back onto the ticket row is what the console reads."""

    async def test_order_reference_is_persisted_to_the_ticket(self, runtime) -> None:
        async with session_scope(runtime.session_factory) as session:
            ticket = TicketRow(
                external_id="E-ORDREF",
                from_email="c@example.com",
                subject="Where is my order?",
                body="Any update on ORD-400013 please?",
            )
            session.add(ticket)
            await session.flush()
            ticket_id = ticket.id

        await runtime.bus.publish(Topics.TICKET_RECEIVED, {"ticket_id": ticket_id})
        await AgentWorker(runtime, name="t").run_once()

        async with session_scope(runtime.session_factory) as session:
            stored = await session.get(TicketRow, ticket_id)
            assert stored is not None
            assert stored.order_ref == "ORD-400013"
            assert stored.intent == "order_status"


class TestIdleQueueIsNotAFailure:
    """A blocking read that finds nothing is the normal state of a drained worker.

    Redis Streams' BLOCK window and the client's socket deadline expire at
    essentially the same moment, so an idle worker used to raise `TimeoutError`
    out of `consume`, which `run_forever` caught and logged as
    `worker.cycle_failed` — an error line every few seconds, forever, burying
    the failures that actually matter.
    """

    async def test_blocking_read_timeout_reads_as_no_work(self) -> None:
        class TimingOutRedis:
            async def xgroup_create(self, *a: object, **k: object) -> None:
                return None

            async def xreadgroup(self, *a: object, **k: object) -> object:
                raise RedisTimeoutError("Timeout reading from redis:6379")

        bus = RedisStreamBus(cast(Any, TimingOutRedis()))
        assert await bus.consume("tickets.received", "worker-1") == []

    async def test_real_errors_still_propagate(self) -> None:
        class FailingRedis:
            async def xgroup_create(self, *a: object, **k: object) -> None:
                return None

            async def xreadgroup(self, *a: object, **k: object) -> object:
                raise ConnectionError("connection refused")

        bus = RedisStreamBus(cast(Any, FailingRedis()))
        with pytest.raises(ConnectionError):
            await bus.consume("tickets.received", "worker-1")
