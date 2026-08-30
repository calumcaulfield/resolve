"""In-memory bus for tests and the single-process demo.

Implements the same read/ack/claim semantics as the Redis implementation,
including redelivery of messages that were never acknowledged — which is what
lets the crash-recovery tests run without Redis.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any

from resolve.bus.base import Event


class InMemoryBus:
    def __init__(self, claim_idle_seconds: float = 30.0) -> None:
        self.claim_idle_seconds = claim_idle_seconds
        self._streams: dict[str, list[Event]] = defaultdict(list)
        self._pending: dict[str, dict[str, tuple[Event, float, str]]] = defaultdict(dict)
        self.delivered: list[Event] = []

    async def publish(
        self, topic: str, payload: dict[str, Any], *, correlation_id: str = ""
    ) -> str:
        message_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        event = Event(
            topic=topic,
            payload=payload,
            message_id=message_id,
            correlation_id=correlation_id or str(payload.get("correlation_id", "")),
        )
        self._streams[topic].append(event)
        return message_id

    async def consume(self, topic: str, consumer: str, *, count: int = 10) -> list[Event]:
        batch = self._streams[topic][:count]
        self._streams[topic] = self._streams[topic][count:]
        now = time.time()
        for event in batch:
            self._pending[topic][event.message_id] = (event, now, consumer)
            self.delivered.append(event)
        return batch

    async def ack(self, topic: str, message_id: str) -> None:
        self._pending[topic].pop(message_id, None)

    async def claim_stale(self, topic: str, consumer: str, *, count: int = 10) -> list[Event]:
        now = time.time()
        claimed: list[Event] = []
        for message_id, (event, delivered_at, owner) in list(self._pending[topic].items()):
            if owner == consumer or now - delivered_at < self.claim_idle_seconds:
                continue
            event.attempts += 1
            self._pending[topic][message_id] = (event, now, consumer)
            claimed.append(event)
            if len(claimed) >= count:
                break
        return claimed

    async def pending_count(self, topic: str) -> int:
        return len(self._pending[topic])

    def depth(self, topic: str) -> int:
        return len(self._streams[topic])
