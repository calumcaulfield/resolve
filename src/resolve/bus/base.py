"""Event bus abstraction.

Two implementations satisfy one protocol: Redis Streams in production,
in-memory for tests. The consumer contract is the important part and it is the
same for both — **read, process, then acknowledge**. A message that is not
acknowledged becomes claimable by another consumer after an idle timeout.

This is the specific defect the portfolio audit found in the original payment
control plane, where `GET /jobs/next` removed the job from an in-memory array
before the worker had done anything with it. A worker crash meant the job was
gone with no record it had ever existed.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Event(BaseModel):
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)
    message_id: str = ""
    correlation_id: str = ""
    attempts: int = 0


@runtime_checkable
class Bus(Protocol):
    async def publish(
        self, topic: str, payload: dict[str, Any], *, correlation_id: str = ""
    ) -> str: ...
    async def consume(self, topic: str, consumer: str, *, count: int = 10) -> list[Event]: ...
    async def ack(self, topic: str, message_id: str) -> None: ...
    async def claim_stale(self, topic: str, consumer: str, *, count: int = 10) -> list[Event]: ...
    async def pending_count(self, topic: str) -> int: ...


class Topics:
    """Stream names. Transport-agnostic, so importing them never pulls in a
    client library — the in-memory bus and Redis use exactly the same names."""

    TICKET_RECEIVED = "resolve.tickets.received"
    TICKET_TRIAGED = "resolve.tickets.triaged"
    TICKET_RESOLVED = "resolve.tickets.resolved"
    APPROVAL_REQUESTED = "resolve.approvals.requested"
    APPROVAL_DECIDED = "resolve.approvals.decided"
