"""Outbox dispatcher.

Delivers rows from the `outbox` table to customer webhooks. Every delivery is:

* **egress-checked** — the target URL must pass the allowlist, closing the
  SSRF hole (finding S-3) that the audited original had;
* **HMAC-signed** with a timestamp, so the receiver can verify authenticity
  and reject replays;
* **retried** with exponential backoff, and parked as `dead_letter` after the
  attempt ceiling rather than retried forever.

Because the outbox row is written in the same transaction as the state change
that produced it, there is no window in which the state changed but the
notification was silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from resolve.api.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_payload
from resolve.db.models import OutboxMessage
from resolve.db.session import session_scope
from resolve.logging import configure_logging, get_logger
from resolve.runtime import Runtime, build_runtime
from resolve.safety.egress import EgressGuard, EgressViolationError
from resolve.telemetry import OUTBOX_PENDING

log = get_logger(__name__)

MAX_ATTEMPTS = 6


class OutboxDispatcher:
    def __init__(self, runtime: Runtime, client: httpx.AsyncClient | None = None) -> None:
        self.runtime = runtime
        self.guard = EgressGuard(runtime.settings.security.webhook_allowed_hosts)
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        self._stopping.set()

    @staticmethod
    def _backoff(attempts: int) -> timedelta:
        return timedelta(seconds=min(2**attempts, 300))

    async def dispatch_once(self, batch_size: int = 20) -> int:
        now = datetime.now(UTC)
        async with session_scope(self.runtime.session_factory) as session:
            rows = (
                (
                    await session.execute(
                        select(OutboxMessage)
                        .where(
                            OutboxMessage.status == "pending",
                            OutboxMessage.next_attempt_at <= now,
                        )
                        .order_by(OutboxMessage.created_at)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )

            for row in rows:
                row.attempts += 1
                try:
                    self.guard.check(row.target_url)
                except EgressViolationError as exc:
                    # Never retried: the target is not allowed to be called at
                    # all, so retrying is pointless and dangerous.
                    row.status = "rejected"
                    row.last_error = f"egress rejected: {exc}"
                    log.error("outbox.egress_rejected", url=row.target_url, error=str(exc))
                    continue

                timestamp, signature = sign_payload(
                    row.payload, self.runtime.settings.security.hmac_secret
                )
                try:
                    response = await self.client.post(
                        row.target_url,
                        json=row.payload,
                        headers={
                            SIGNATURE_HEADER: signature,
                            TIMESTAMP_HEADER: timestamp,
                            "content-type": "application/json",
                        },
                    )
                    response.raise_for_status()
                except Exception as exc:
                    row.last_error = f"{type(exc).__name__}: {exc}"
                    if row.attempts >= MAX_ATTEMPTS:
                        row.status = "dead_letter"
                        log.error(
                            "outbox.dead_letter",
                            id=row.id,
                            attempts=row.attempts,
                            error=row.last_error,
                        )
                    else:
                        row.next_attempt_at = now + self._backoff(row.attempts)
                    continue

                row.status = "delivered"
                row.delivered_at = now
                row.last_error = None
                log.info("outbox.delivered", id=row.id, topic=row.topic)

            pending = (
                (
                    await session.execute(
                        select(OutboxMessage).where(OutboxMessage.status == "pending")
                    )
                )
                .scalars()
                .all()
            )
            OUTBOX_PENDING.set(len(pending))

        return len(rows)

    async def run_forever(self, interval: float = 2.0) -> None:
        log.info("outbox.started")
        while not self._stopping.is_set():
            try:
                handled = await self.dispatch_once()
            except Exception as exc:
                log.error("outbox.cycle_failed", error=f"{type(exc).__name__}: {exc}")
                handled = 0
            if handled == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=interval)
        await self.client.aclose()
        log.info("outbox.stopped")


async def main() -> int:
    runtime = await build_runtime()
    configure_logging(runtime.settings.log_level, runtime.settings.log_json)
    dispatcher = OutboxDispatcher(runtime)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, dispatcher.request_stop)

    try:
        await dispatcher.run_forever()
    finally:
        await runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
