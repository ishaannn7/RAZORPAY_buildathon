"""Declarative base and shared column conventions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, MetaData, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint naming so Alembic can autogenerate stable migrations
# against both SQLite and PostgreSQL.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware datetime that round-trips correctly on SQLite.

    SQLite discards tzinfo, so a naive value read back from the database would
    silently compare unequal to an aware one. Timestamps are normalized to UTC
    on write and re-tagged as UTC on read.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("refusing to store a naive datetime; attach a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """Persist a :class:`StrEnum` as text and rehydrate it as the enum.

    Stored as its ``value`` rather than its ``name`` so the database is readable
    and stable across renames. Rehydration matters: a bare string read back from
    the database compares equal to a ``StrEnum`` member but fails an ``is``
    identity check and has no ``.value``, so without this the domain logic would
    silently take the wrong branch.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[E], length: int = 48) -> None:
        super().__init__(length=length)
        self.enum_class = enum_class

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Validate on the way in: an unknown value must fail here rather than
        # become an unmatchable row later.
        return self.enum_class(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        if value is None:
            return None
        return self.enum_class(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def new_id() -> str:
    """Generate a sortable-enough opaque identifier."""
    return uuid.uuid4().hex


def utc_now() -> datetime:
    return datetime.now(UTC)


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, nullable=False)
