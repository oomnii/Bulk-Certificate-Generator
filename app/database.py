"""SQLAlchemy engine and session management."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

SessionLocal: sessionmaker[Session] | None = None
_engine: Engine | None = None

_CREDENTIAL_RE = re.compile(r"://([^:/]+):([^@]+)@")


def redact_database_url(url: str) -> str:
    """Hide passwords if a future PostgreSQL URL contains them."""
    return _CREDENTIAL_RE.sub(r"://\1:***@", url)


def configure_database(settings: Settings) -> None:
    """Create the process-wide engine. Safe to call again in tests."""
    global SessionLocal, _engine
    if _engine is not None:
        _engine.dispose()

    connect_args: dict[str, object] = {}
    if settings.is_sqlite:
        # The API and worker are separate processes. A busy timeout keeps the
        # second writer waiting instead of failing immediately on SQLite's lock.
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30

    _engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
    if settings.is_sqlite:
        event.listen(_engine, "connect", _sqlite_on_connect)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _sqlite_on_connect(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def init_db() -> None:
    if _engine is None:
        raise RuntimeError("Database is not configured")
    from app.models import Base

    Base.metadata.create_all(_engine)


def dispose_database() -> None:
    global SessionLocal, _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
    SessionLocal = None


def get_session_factory() -> sessionmaker[Session]:
    if SessionLocal is None:
        raise RuntimeError("Database is not configured")
    return SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Open a session, commit on success, and always roll back failures.

    Each call uses a new session so a failed commit cannot poison the next
    recipient processed by the worker.
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
    """FastAPI dependency. Callers that write data commit explicitly."""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
