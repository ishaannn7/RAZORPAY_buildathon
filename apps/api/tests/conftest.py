"""Shared fixtures.

Each test gets its own SQLite file and data directory so tests can run in any
order without sharing state, and so a failure leaves an inspectable database
behind rather than a mystery.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from reconproof.config import Settings, get_settings
from reconproof.db import session as db_session


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("RECONPROOF_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RECONPROOF_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()
    db_session.reset_engine()
    resolved = get_settings()
    db_session.create_all()
    yield resolved
    db_session.reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def db(settings: Settings) -> Iterator[Session]:
    factory = db_session.get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
