"""The connector must not be able to write. Prove it from several angles."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from auditor.ingestion.connector import (
    WriteAttemptBlocked,
    _looks_like_read,
)


# --- statement guard (no DB needed) ---------------------------------------- #

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select * from users",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "EXPLAIN SELECT * FROM orders",
    "/* comment */\nSELECT now()",
    "SHOW transaction_read_only",
])
def test_guard_allows_reads(sql):
    assert _looks_like_read(sql) is True


@pytest.mark.parametrize("sql", [
    "INSERT INTO users (email) VALUES ('x')",
    "UPDATE users SET email = 'x'",
    "DELETE FROM users",
    "DROP TABLE users",
    "ALTER TABLE users ADD COLUMN x int",
    "TRUNCATE users",
    "CREATE TEMP TABLE t (x int)",
    "GRANT SELECT ON users TO x",
    "COPY users FROM STDIN",
    "EXPLAIN ANALYZE INSERT INTO users DEFAULT VALUES",
    "DO $$ BEGIN PERFORM 1; END $$",
])
def test_guard_blocks_writes(sql):
    assert _looks_like_read(sql) is False


# --- against the live read-only role -------------------------------------- #

def test_report_is_read_only(connector):
    report = connector.report
    assert report.ok, report.summary()
    assert report.is_superuser is False
    assert report.can_create_in_db is False
    assert report.writable_tables == []
    assert report.session_read_only is True
    assert report.probe_write_blocked is True


def test_python_guard_blocks_write_through_connect(connector):
    with connector.connect() as db:
        with pytest.raises(WriteAttemptBlocked):
            db.execute(text("INSERT INTO users (email) VALUES ('nope@example.com')"))


def test_database_itself_rejects_write(connector):
    """Bypass our Python guard entirely - raw driver cursor - and confirm
    PostgreSQL still refuses the write."""
    raw = connector.engine.raw_connection()
    try:
        cur = raw.cursor()
        with pytest.raises(Exception) as excinfo:
            cur.execute("INSERT INTO users (email, password_hash, full_name) "
                        "VALUES ('x@x.com', 'h', 'X')")
        raw.rollback()
        msg = str(excinfo.value).lower()
        assert "read-only" in msg or "permission denied" in msg
    finally:
        raw.close()


def test_reads_still_work(connector):
    with connector.connect() as db:
        assert db.execute(text("SELECT count(*) FROM users")).scalar_one() > 0
