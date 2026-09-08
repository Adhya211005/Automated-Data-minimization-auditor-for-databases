"""SQLite engine / session management for audit history.

DB location: ``$AUDIT_HISTORY_DB`` or ``auditor/audit_history.db``.
Tests set ``AUDIT_HISTORY_DB`` to a temp path (or ``:memory:``).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from auditor.storage.models import Base

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "audit_history.db"

_engine: Engine | None = None
_Session: sessionmaker | None = None


def _database_url() -> str:
    raw = os.environ.get("AUDIT_HISTORY_DB")
    if not raw:
        return f"sqlite:///{_DEFAULT_PATH}"
    if raw in (":memory:", "sqlite://"):
        return "sqlite://"
    return raw if raw.startswith("sqlite") else f"sqlite:///{raw}"


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = _database_url()
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url == "sqlite://":       # shared in-memory across sessions
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, **kwargs)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(_engine)
    return _engine


def init_db() -> None:
    get_engine()


def reset_engine() -> None:
    """Drop the cached engine (tests switching DB paths)."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


@contextmanager
def get_session() -> Iterator[Session]:
    get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
