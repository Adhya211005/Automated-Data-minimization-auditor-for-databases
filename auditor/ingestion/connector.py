"""Read-only connection to a target database.

Three independent layers stop the auditor from ever writing to a database it
scans (CLAUDE.md). Any one of them is enough; all three are on:

  1. The role.  ``dma_auditor_ro`` holds only CONNECT + USAGE + SELECT
     (migrations/001). PostgreSQL itself rejects any write.
  2. The session.  Every connection sets ``default_transaction_read_only = on``,
     so writes fail even on a mis-granted table.
  3. The statement guard.  A SQLAlchemy hook inspects every statement before it
     runs and raises ``WriteAttemptBlocked`` for anything that isn't a read.

``ReadOnlyConnector`` verifies 1 and 2 at construction time (it actually tries
to write and confirms the database refuses) and fails closed otherwise.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from sqlalchemy import event, text
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.exc import DBAPIError

from auditor.config import TargetConfig


class ReadOnlyViolation(RuntimeError):
    """The connection is not safely read-only - refuse to use it."""


class WriteAttemptBlocked(RuntimeError):
    """Auditor code tried to run a non-read statement through the connector."""


# First keyword of a statement that is safe to send over a read-only connection.
# Transaction-control verbs are allowed because SQLAlchemy/psycopg emit them.
_READ_KEYWORDS = frozenset(
    """
    select with explain show table values
    begin start commit rollback savepoint release end
    set reset discard fetch move close declare
    prepare deallocate
    """.split()
)

_LEADING_NOISE = re.compile(r"^(?:\s|/\*.*?\*/|--[^\n]*\n)+", re.DOTALL)
_FIRST_WORD = re.compile(r"^\s*([a-zA-Z]+)")


def _first_keyword(sql: str) -> str:
    stripped = _LEADING_NOISE.sub("", sql or "")
    m = _FIRST_WORD.match(stripped)
    return m.group(1).lower() if m else ""


def _looks_like_read(sql: str) -> bool:
    kw = _first_keyword(sql)
    if kw not in _READ_KEYWORDS:
        return False
    if kw == "explain" and re.search(r"\bexplain\b.*\banalyze\b", sql, re.I | re.S):
        return False  # EXPLAIN ANALYZE actually executes the inner statement
    return True


def make_engine(cfg: TargetConfig, *, echo: bool = False) -> Engine:
    """Build an Engine whose every connection is read-only, with the statement
    guard attached. Does not itself verify the role - use ReadOnlyConnector."""
    if cfg.dialect == "postgresql":
        connect_args = {"options": "-c default_transaction_read_only=on -c application_name=dma_auditor"}
    else:  # mysql and friends - session flag set in a connect event below
        connect_args = {}

    engine = create_engine(
        cfg.sqlalchemy_url(),
        echo=echo,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )

    if cfg.dialect != "postgresql":
        @event.listens_for(engine, "connect")
        def _force_ro(dbapi_conn, _rec):  # pragma: no cover - non-pg path
            cur = dbapi_conn.cursor()
            try:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            finally:
                cur.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):
        if not _looks_like_read(statement):
            raise WriteAttemptBlocked(
                f"blocked non-read statement: {statement.strip()[:120]!r}"
            )

    return engine


@dataclass
class ReadOnlyReport:
    """Result of probing whether a connection can write."""

    role: str
    is_superuser: bool
    can_create_in_db: bool
    session_read_only: bool
    writable_tables: list[str] = field(default_factory=list)
    probe_write_blocked: bool = False
    probe_detail: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.is_superuser
            and not self.can_create_in_db
            and not self.writable_tables
            and self.session_read_only
            and self.probe_write_blocked
        )

    def summary(self) -> str:
        lines = [
            f"role                = {self.role}",
            f"is_superuser        = {self.is_superuser}",
            f"can_create_in_db    = {self.can_create_in_db}",
            f"session_read_only   = {self.session_read_only}",
            f"writable_tables     = {self.writable_tables or 'none'}",
            f"probe_write_blocked = {self.probe_write_blocked}  ({self.probe_detail})",
            f"VERDICT             = {'READ-ONLY OK' if self.ok else 'NOT READ-ONLY - refusing'}",
        ]
        return "\n".join(lines)


def _verify_postgres(engine: Engine) -> ReadOnlyReport:
    with engine.connect() as conn:
        role = conn.execute(text("SELECT current_user")).scalar_one()
        is_super = conn.execute(
            text("SELECT current_setting('is_superuser') = 'on'")
        ).scalar_one()
        can_create = conn.execute(
            text("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")
        ).scalar_one()
        session_ro = conn.execute(
            text("SELECT current_setting('transaction_read_only') = 'on'")
        ).scalar_one()
        writable = list(
            conn.execute(
                text(
                    """
                    SELECT format('%I.%I', schemaname, tablename) AS t
                    FROM pg_tables
                    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                      AND (
                        has_table_privilege(current_user, schemaname||'.'||tablename, 'INSERT')
                        OR has_table_privilege(current_user, schemaname||'.'||tablename, 'UPDATE')
                        OR has_table_privilege(current_user, schemaname||'.'||tablename, 'DELETE')
                        OR has_table_privilege(current_user, schemaname||'.'||tablename, 'TRUNCATE')
                      )
                    ORDER BY 1
                    """
                )
            ).scalars()
        )

    # Actually try to write, bypassing our Python guard (raw DBAPI cursor), so
    # this tests the database, not our own code.
    blocked, detail = _probe_write(engine)

    return ReadOnlyReport(
        role=role,
        is_superuser=bool(is_super),
        can_create_in_db=bool(can_create),
        session_read_only=bool(session_ro),
        writable_tables=writable,
        probe_write_blocked=blocked,
        probe_detail=detail,
    )


def _probe_write(engine: Engine) -> tuple[bool, str]:
    """Actually try to write and confirm the database refuses. Uses a raw
    connection so the SQLAlchemy statement guard does not intercept it, and
    probes twice: once in the default (read-only) session, and once after
    forcing the session READ WRITE so the *privilege* is what's tested, not
    just the transaction flag."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()

        def attempt(label: str) -> tuple[bool, str]:
            try:
                cur.execute("CREATE TEMP TABLE _dma_probe_ (x int)")
            except Exception as exc:  # noqa: BLE001 - any failure means "blocked"
                raw.rollback()
                return True, f"{label}: {type(exc).__name__}"
            # Somehow succeeded - the connection is writable. Clean up.
            try:
                cur.execute("DROP TABLE _dma_probe_")
            except Exception:  # noqa: BLE001
                pass
            raw.rollback()
            return False, f"{label}: SUCCEEDED (writable!)"

        blocked1, d1 = attempt("default session")
        try:
            # COMMIT (not rollback) so the SESSION setting sticks past this txn;
            # COMMIT of a read-only transaction is itself allowed.
            cur.execute("SET SESSION default_transaction_read_only = off")
            raw.commit()
            rw = cur.execute("SHOW transaction_read_only").fetchone()[0] == "off"
        except Exception:  # noqa: BLE001
            raw.rollback()
            rw = False
        blocked2, d2 = attempt("forced read-write" if rw else "read-write (flip failed)")

        return (blocked1 and blocked2), f"{d1}; {d2}"
    finally:
        raw.close()


class ReadOnlyConnector:
    """A verified read-only handle to a target database.

    Construction fails with ``ReadOnlyViolation`` unless the database itself
    refuses writes. Use it as the single entry point for all target-DB access::

        conn = ReadOnlyConnector.from_env()
        with conn.connect() as db:
            rows = db.execute(text("SELECT ...")).all()
    """

    def __init__(self, config: TargetConfig, *, echo: bool = False, strict: bool = True):
        self.config = config
        self.engine = make_engine(config, echo=echo)
        try:
            self.report = self.verify()
        except DBAPIError as exc:
            self.engine.dispose()
            raise ReadOnlyViolation(f"could not connect to {config.safe_repr()}: {exc}") from exc
        if strict and not self.report.ok:
            self.engine.dispose()
            raise ReadOnlyViolation(
                "target connection is not safely read-only:\n" + self.report.summary()
            )

    @classmethod
    def from_env(cls, *, echo: bool = False, strict: bool = True, **overrides) -> "ReadOnlyConnector":
        return cls(TargetConfig.from_env(**overrides), echo=echo, strict=strict)

    def verify(self) -> ReadOnlyReport:
        if self.config.dialect == "postgresql":
            return _verify_postgres(self.engine)
        raise NotImplementedError(f"read-only verification not implemented for {self.config.dialect!r}")

    @contextmanager
    def connect(self) -> Iterator:
        """Yield a Connection inside a read-only transaction that is always
        rolled back (never committed)."""
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                yield conn
            finally:
                trans.rollback()

    def dispose(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> "ReadOnlyConnector":
        return self

    def __exit__(self, *exc) -> None:
        self.dispose()
