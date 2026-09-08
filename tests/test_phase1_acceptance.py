"""Phase 1 acceptance check: the read-only guarantee.

Connects to the target database *directly* as ``dma_auditor_ro`` (not through
``ReadOnlyConnector``, so this tests the database grant itself, not our Python
guard) and asserts:

  * SELECT on every seed table succeeds
  * INSERT / UPDATE / DELETE / TRUNCATE / DROP / CREATE all fail
  * they still fail with a *permission* error (SQLSTATE 42501) even after the
    session is explicitly switched to READ WRITE - i.e. the role genuinely
    holds no write privilege, it isn't just a read-only transaction flag

Run as a test:      pytest tests/test_phase1_acceptance.py -v
Run standalone:     python tests/test_phase1_acceptance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

if __package__ in (None, ""):  # running as `python tests/test_phase1_acceptance.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auditor.config import TargetConfig

# statement -> SQL. Each one must be rejected for dma_auditor_ro.
WRITE_STATEMENTS: dict[str, str] = {
    "INSERT": "INSERT INTO users (email, password_hash, full_name) "
              "VALUES ('phase1-acceptance@test.invalid', 'x', 'Acceptance Probe')",
    "UPDATE": "UPDATE users SET email = email WHERE id = (SELECT min(id) FROM users)",
    "DELETE": "DELETE FROM users WHERE id = -999999",
    "TRUNCATE": "TRUNCATE support_tickets",
    "DROP": "DROP TABLE support_tickets",
    "CREATE": "CREATE TABLE _phase1_acceptance_probe (x int)",
    "CREATE TEMP": "CREATE TEMP TABLE _phase1_acceptance_tmp (x int)",
}

SEED_TABLES = ("users", "orders", "support_tickets")

PERMISSION_DENIED = "42501"          # insufficient_privilege
READ_ONLY_TXN = "25006"             # read_only_sql_transaction


def _connect(cfg: TargetConfig) -> psycopg.Connection:
    return psycopg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, dbname=cfg.dbname, autocommit=True,
    )


@pytest.fixture(scope="module")
def ro_conn():
    cfg = TargetConfig.from_env()
    try:
        conn = _connect(cfg)
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"cannot connect as {cfg.user}: {exc}\n"
            "Run:  psql -U postgres -f migrations/001_create_readonly_role.sql"
        )
    yield conn
    conn.close()


# --------------------------------------------------------------------------- #

def test_connected_as_readonly_role(ro_conn):
    who = ro_conn.execute("SELECT current_user").fetchone()[0]
    assert who == "dma_auditor_ro"


def test_select_succeeds_on_every_seed_table(ro_conn):
    for table in SEED_TABLES:
        n = ro_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert n > 0, f"{table} is empty - run seed-data/seed.py --reset"


@pytest.mark.parametrize("name", list(WRITE_STATEMENTS))
def test_write_is_blocked_in_default_session(ro_conn, name):
    """Default session: the write is rejected (read-only txn or no privilege)."""
    with pytest.raises(psycopg.Error) as excinfo:
        ro_conn.execute(WRITE_STATEMENTS[name])
    assert excinfo.value.sqlstate in {PERMISSION_DENIED, READ_ONLY_TXN}, \
        f"{name}: unexpected SQLSTATE {excinfo.value.sqlstate}"


@pytest.mark.parametrize("name", ["INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "CREATE"])
def test_write_is_permission_denied_even_when_read_write(ro_conn, name):
    """Force the session READ WRITE, then the write must STILL fail - and now
    specifically with 42501, proving the grant (not the txn flag) blocks it."""
    ro_conn.execute("SET default_transaction_read_only = off")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
            ro_conn.execute(WRITE_STATEMENTS[name])
        assert excinfo.value.sqlstate == PERMISSION_DENIED
    finally:
        ro_conn.execute("SET default_transaction_read_only = on")


def test_select_still_works_after_the_write_attempts(ro_conn):
    assert ro_conn.execute("SELECT 1").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest) - prints a PASS/FAIL table.
# --------------------------------------------------------------------------- #

def _run_standalone() -> int:
    cfg = TargetConfig.from_env()
    print(f"connecting as {cfg.user}@{cfg.host}:{cfg.port}/{cfg.dbname}\n")
    try:
        conn = _connect(cfg)
    except psycopg.OperationalError as exc:
        print(f"FAIL: cannot connect: {exc}")
        print("Run:  psql -U postgres -f migrations/001_create_readonly_role.sql")
        return 2

    passed = failed = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        mark = "PASS" if ok else "FAIL"
        passed += ok
        failed += not ok
        print(f"  [{mark}] {label}{'  - ' + detail if detail else ''}")

    who = conn.execute("SELECT current_user").fetchone()[0]
    check("connected as dma_auditor_ro", who == "dma_auditor_ro", who)

    print("\nSELECT (expected to succeed):")
    for table in SEED_TABLES:
        try:
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            check(f"SELECT FROM {table}", n > 0, f"{n} rows")
        except psycopg.Error as exc:
            check(f"SELECT FROM {table}", False, f"{exc.sqlstate} {exc}")

    print("\nWrites in the normal session (expected: rejected):")
    for name, sql in WRITE_STATEMENTS.items():
        try:
            conn.execute(sql)
            check(name, False, "SUCCEEDED - not read-only!")
        except psycopg.Error as exc:
            check(name, exc.sqlstate in {PERMISSION_DENIED, READ_ONLY_TXN}, f"SQLSTATE {exc.sqlstate}")

    print("\nWrites with session forced READ WRITE (expected: permission denied, 42501):")
    conn.execute("SET default_transaction_read_only = off")
    for name in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "CREATE"):
        try:
            conn.execute(WRITE_STATEMENTS[name])
            check(name, False, "SUCCEEDED - role can write!")
        except psycopg.errors.InsufficientPrivilege as exc:
            check(name, True, f"SQLSTATE {exc.sqlstate}")
        except psycopg.Error as exc:
            check(name, False, f"wrong error: SQLSTATE {exc.sqlstate}")
    conn.execute("SET default_transaction_read_only = on")

    conn.close()
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
