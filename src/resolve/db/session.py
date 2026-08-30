"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from resolve.config import DatabaseSettings
from resolve.db.base import Base


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": settings.echo, "future": True}
    # SQLite (used by tests) does not support connection pooling options.
    if not settings.url.startswith("sqlite"):
        kwargs.update(pool_size=settings.pool_size, max_overflow=settings.max_overflow)
    return create_async_engine(settings.url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


#: Arbitrary but fixed. Every process that might create the schema uses the
#: same key, so they serialise against each other and nothing else.
_SCHEMA_LOCK_KEY = 0x5265_736F  # "Reso"


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema, safely under concurrent startup.

    Production uses Alembic migrations; see `alembic/` and `make migrate`. This
    exists for tests and the demo stack.

    The advisory lock is not decoration. `docker compose up` starts the API,
    two agent-worker replicas and the outbox dispatcher at the same moment, and
    all four call this. SQLAlchemy's `create_all` is `checkfirst=True`, but
    "check then create" is not atomic: two processes can both observe a missing
    table and both issue `CREATE TABLE`, at which point Postgres fails one of
    them with `duplicate key value violates unique constraint
    "pg_type_typname_nsp_index"` and that process exits.

    It is a race, so it appears intermittently and disproportionately on a
    clean volume, which is the worst way for a bug to behave. A transaction-
    scoped advisory lock makes the whole check-and-create atomic across
    processes; the lock is released automatically when the transaction ends,
    including if the process dies holding it.
    """
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK_KEY}
            )
        # SQLite needs no lock: it serialises writers itself, and the test
        # suite gives every test its own database file.
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
