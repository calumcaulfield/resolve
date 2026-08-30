"""Redis Streams bus.

Consumer groups give per-message acknowledgement and, crucially, `XAUTOCLAIM`:
a message a dead worker never acknowledged is automatically reassigned after
an idle timeout instead of being lost. Combined with the durable workflow
engine's step checkpointing, redelivery is safe — the resumed run skips every
step that already succeeded.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from resolve.bus.base import Event
from resolve.logging import get_logger

log = get_logger(__name__)


class RedisStreamBus:
    def __init__(
        self,
        redis: Redis,
        *,
        group: str = "resolve",
        claim_idle_ms: int = 60_000,
        block_ms: int = 5_000,
        maxlen: int = 100_000,
    ) -> None:
        self.redis = redis
        self.group = group
        self.claim_idle_ms = claim_idle_ms
        self.block_ms = block_ms
        self.maxlen = maxlen
        self._groups_ready: set[str] = set()

    async def _ensure_group(self, topic: str) -> None:
        if topic in self._groups_ready:
            return
        try:
            await self.redis.xgroup_create(topic, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups_ready.add(topic)

    async def publish(
        self, topic: str, payload: dict[str, Any], *, correlation_id: str = ""
    ) -> str:
        await self._ensure_group(topic)
        # `approximate=True` keeps trimming O(1); the stream is a transport,
        # not the system of record — Postgres is.
        message_id = await self.redis.xadd(
            topic,
            {
                "payload": json.dumps(payload, default=str),
                "correlation_id": correlation_id or str(payload.get("correlation_id", "")),
            },
            maxlen=self.maxlen,
            approximate=True,
        )
        return str(message_id)

    @staticmethod
    def _to_event(topic: str, message_id: Any, fields: dict[Any, Any], attempts: int = 0) -> Event:
        def _get(key: str) -> str:
            value = fields.get(key) or fields.get(key.encode())
            if isinstance(value, bytes):
                return value.decode()
            return str(value or "")

        return Event(
            topic=topic,
            payload=json.loads(_get("payload") or "{}"),
            message_id=message_id.decode() if isinstance(message_id, bytes) else str(message_id),
            correlation_id=_get("correlation_id"),
            attempts=attempts,
        )

    async def consume(self, topic: str, consumer: str, *, count: int = 10) -> list[Event]:
        await self._ensure_group(topic)
        response: Any
        try:
            response = await self.redis.xreadgroup(
                self.group, consumer, {topic: ">"}, count=count, block=self.block_ms
            )
        except RedisTimeoutError:
            # A blocking read that reaches its deadline means the queue was idle,
            # which is the normal state of a drained worker — not a failure. The
            # client's socket deadline and the server's BLOCK window expire at
            # essentially the same moment, so without this an idle worker logs an
            # error every cycle and buries the failures that actually matter.
            return []
        events: list[Event] = []
        for _stream, messages in response or []:
            for message_id, fields in messages:
                events.append(self._to_event(topic, message_id, fields))
        return events

    async def ack(self, topic: str, message_id: str) -> None:
        await self.redis.xack(topic, self.group, message_id)

    async def claim_stale(self, topic: str, consumer: str, *, count: int = 10) -> list[Event]:
        """Take over messages a dead consumer never acknowledged."""
        await self._ensure_group(topic)
        _cursor, messages, _deleted = await self.redis.xautoclaim(
            topic, self.group, consumer, min_idle_time=self.claim_idle_ms, count=count
        )
        events = [self._to_event(topic, mid, fields, attempts=1) for mid, fields in messages]
        if events:
            log.warning("bus.reclaimed", topic=topic, consumer=consumer, count=len(events))
        return events

    async def pending_count(self, topic: str) -> int:
        await self._ensure_group(topic)
        info = await self.redis.xpending(topic, self.group)
        return int(info.get("pending", 0)) if isinstance(info, dict) else int(info[0] or 0)
