"""Service runtime: the composition root for the long-running processes.

`Application` (bootstrap.py) is the pure, in-memory core used by the CLI, the
tests and the evaluation harness. `Runtime` adds the stateful infrastructure —
database, event bus, workflow engine — that the API and workers need.

Keeping them separate is what allows the entire agent and its evaluation to
run with no Postgres and no Redis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from resolve.bootstrap import Application, build_application
from resolve.bus.base import Bus
from resolve.bus.memory import InMemoryBus
from resolve.config import Settings, get_settings
from resolve.db.session import create_all, create_engine, create_session_factory
from resolve.logging import configure_logging, get_logger
from resolve.telemetry import configure_telemetry
from resolve.workflow.definitions import RESOLVE_TICKET, build_resolve_ticket_workflow
from resolve.workflow.engine import WorkflowEngine

log = get_logger(__name__)


@dataclass
class Runtime:
    settings: Settings
    application: Application
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    workflows: WorkflowEngine
    bus: Bus

    async def close(self) -> None:
        await self.engine.dispose()
        redis = getattr(self.bus, "redis", None)
        if redis is not None:
            await redis.aclose()


async def build_runtime(
    settings: Settings | None = None,
    *,
    create_schema: bool = True,
    bus: Bus | None = None,
    **application_kwargs: Any,
) -> Runtime:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    configure_telemetry(settings.otel)
    settings.assert_production_safe()

    application = await build_application(settings, **application_kwargs)

    db_engine = create_engine(settings.db)
    session_factory = create_session_factory(db_engine)
    if create_schema:
        await create_all(db_engine)

    workflows = WorkflowEngine(session_factory)
    workflows.register(
        RESOLVE_TICKET,
        build_resolve_ticket_workflow(
            application.agent, session_factory, application.llm.provider.name
        ),
    )

    resolved_bus = bus or _build_bus(settings)

    log.info(
        "runtime.ready",
        provider=settings.llm.provider,
        bus=type(resolved_bus).__name__,
        database=settings.db.url.split("@")[-1],
    )
    return Runtime(
        settings=settings,
        application=application,
        engine=db_engine,
        session_factory=session_factory,
        workflows=workflows,
        bus=resolved_bus,
    )


def _build_bus(settings: Settings) -> Bus:
    try:
        from redis.asyncio import Redis

        from resolve.bus.streams import RedisStreamBus
    except ImportError:  # pragma: no cover - redis is an install extra in dev
        log.warning("runtime.bus.fallback", reason="redis package unavailable")
        return InMemoryBus()

    return RedisStreamBus(
        # The socket deadline must outlast the server's BLOCK window, or every
        # idle read races its own timeout. `consume` tolerates the timeout too;
        # this simply stops it happening on every cycle.
        Redis.from_url(
            settings.redis.url,
            socket_timeout=(settings.redis.block_ms / 1000) + 5.0,
        ),
        group=settings.redis.consumer_group,
        claim_idle_ms=settings.redis.claim_idle_ms,
        block_ms=settings.redis.block_ms,
    )
