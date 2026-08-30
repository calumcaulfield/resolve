"""Agent worker.

Consumes `resolve.tickets.received`, executes the durable ticket-resolution
workflow, and acknowledges the message only after the workflow has committed.

Three properties worth noting, each a direct answer to a finding in the
portfolio audit:

* **Ack after commit.** A worker killed mid-run leaves the message pending,
  and `claim_stale` hands it to another worker after the idle timeout. The
  workflow engine then resumes from the last checkpoint rather than starting
  over. Nothing is lost and nothing consequential is repeated.
* **Graceful drain.** SIGTERM stops the worker taking new work and lets the
  in-flight run finish, instead of abandoning it mid-refund.
* **The failure is recorded.** A run that fails permanently lands in the
  workflow dead-letter state with its error, not in a log line that scrolls
  away.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys

from resolve.bus.base import Event, Topics
from resolve.logging import configure_logging, get_logger, set_correlation_id
from resolve.runtime import Runtime, build_runtime
from resolve.telemetry import QUEUE_DEPTH, span
from resolve.workflow.definitions import RESOLVE_TICKET
from resolve.workflow.engine import WorkflowFailedError

log = get_logger(__name__)


class AgentWorker:
    def __init__(self, runtime: Runtime, name: str | None = None) -> None:
        self.runtime = runtime
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self._stopping = asyncio.Event()
        self.processed = 0
        self.failed = 0

    def request_stop(self) -> None:
        log.info("worker.draining", worker=self.name)
        self._stopping.set()

    async def handle(self, event: Event) -> None:
        ticket_id = str(event.payload.get("ticket_id", ""))
        if not ticket_id:
            log.warning("worker.bad_event", message_id=event.message_id)
            return

        set_correlation_id(event.correlation_id or ticket_id)
        with span("resolve.ticket", ticket_id=ticket_id, worker=self.name):
            try:
                result = await self.runtime.workflows.execute(
                    RESOLVE_TICKET, correlation_id=ticket_id, payload={"ticket_id": ticket_id}
                )
            except WorkflowFailedError as exc:
                self.failed += 1
                log.error(
                    "worker.workflow_dead_letter",
                    ticket_id=ticket_id,
                    step=exc.step,
                    cause=exc.cause,
                )
                return

        self.processed += 1
        await self.runtime.bus.publish(
            Topics.TICKET_RESOLVED,
            {"ticket_id": ticket_id, "status": result.get("status")},
            correlation_id=ticket_id,
        )
        log.info(
            "worker.processed",
            ticket_id=ticket_id,
            status=result.get("status"),
            cost_usd=result.get("cost_usd"),
        )

    async def run_once(self, *, count: int = 10) -> int:
        """One poll cycle: reclaim abandoned work first, then take new work."""
        reclaimed = await self.runtime.bus.claim_stale(
            Topics.TICKET_RECEIVED, self.name, count=count
        )
        events = list(reclaimed)
        if len(events) < count:
            events += await self.runtime.bus.consume(
                Topics.TICKET_RECEIVED, self.name, count=count - len(events)
            )

        for event in events:
            await self.handle(event)
            # Acknowledge only after the workflow has committed.
            await self.runtime.bus.ack(Topics.TICKET_RECEIVED, event.message_id)

        QUEUE_DEPTH.labels(topic=Topics.TICKET_RECEIVED).set(
            await self.runtime.bus.pending_count(Topics.TICKET_RECEIVED)
        )
        return len(events)

    async def run_forever(self, poll_interval: float = 1.0) -> None:
        log.info("worker.started", worker=self.name)
        while not self._stopping.is_set():
            try:
                handled = await self.run_once()
            except Exception as exc:
                log.error("worker.cycle_failed", error=f"{type(exc).__name__}: {exc}")
                handled = 0
            if handled == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll_interval)
        log.info("worker.stopped", worker=self.name, processed=self.processed, failed=self.failed)


async def main() -> int:
    runtime = await build_runtime()
    configure_logging(runtime.settings.log_level, runtime.settings.log_json)
    worker = AgentWorker(runtime)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run_forever()
    finally:
        await runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
