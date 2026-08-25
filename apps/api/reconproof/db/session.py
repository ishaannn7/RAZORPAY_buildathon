"""Engine and session construction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from reconproof.config import get_settings
from reconproof.db.base import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Turn on the SQLite settings this schema actually depends on.

    Foreign keys are off by default in SQLite, which would silently void every
    relationship constraint in the schema. WAL and a busy timeout keep the API
    readable while a batch run holds a write transaction.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.resolved_database_url
        kwargs: dict[str, Any] = {"echo": settings.sql_echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        else:
            kwargs["pool_pre_ping"] = True
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Rolls back on any exception.

    Batch ingestion relies on this: a malformed row partway through a file must
    leave the database exactly as it was, not partially loaded.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def create_all() -> None:
    """Create the schema.

    Alembic owns migrations for PostgreSQL deployments; this exists so the
    zero-dependency SQLite demo path needs no migration step.
    """
    import reconproof.db.models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=get_engine())


def reset_engine() -> None:
    """Drop cached engine and session factory. Used by tests."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
