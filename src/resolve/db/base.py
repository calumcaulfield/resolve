"""SQLAlchemy declarative base and shared column types."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Explicit naming convention so Alembic autogenerates stable constraint names
# and migrations do not churn.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def as_utc(value: datetime | None) -> datetime | None:
    """Coerce a database timestamp to an aware UTC datetime.

    Postgres round-trips `DateTime(timezone=True)` as aware; SQLite — used by
    the test suite — returns the same column naive. Subtracting one from the
    other raises, which is the kind of defect that only appears on the backend
    you did not develop against. Anything doing arithmetic on a row timestamp
    goes through here.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
