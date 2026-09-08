"""Phase 4 - retention policy engine.

Unit tests build synthetic metadata (no DB). The integration tests use the
live seed metadata, where ~18% of users/orders/support_tickets rows were
deliberately seeded 400+ days old.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auditor.ingestion.metadata import ColumnMetadata, DatabaseMetadata, TableMetadata
from auditor.retention import RetentionChecker, RetentionPolicy, is_retention_relevant
from auditor.retention.checker import _age_score

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def tcol(name, gtype="text", *, temporal=False, min_iso=None, max_iso=None):
    return ColumnMetadata(
        table="t", name=name, ordinal=0, generic_type=gtype, raw_type=gtype.upper(),
        nullable=True, is_temporal=temporal, min_value=min_iso, max_value=max_iso,
    )


def one_table_db(columns, *, name="t", rows=100):
    tm = TableMetadata(name=name, schema="public", row_count=rows, columns=[
        ColumnMetadata(**{**c.__dict__, "table": name}) for c in columns
    ])
    return DatabaseMetadata(dialect="postgresql", database="d", schema="public",
                            generated_at=NOW.isoformat(), auditor_version="t", tables=[tm])


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


# --- is_retention_relevant -------------------------------------------- #

@pytest.mark.parametrize("name", ["created_at", "updated_at", "resolved_at", "deleted_at",
                                  "last_login_at", "inserted_at", "event_timestamp"])
def test_lifecycle_timestamps_are_relevant(name):
    assert is_retention_relevant(tcol(name, "timestamptz", temporal=True))


@pytest.mark.parametrize("name", ["date_of_birth", "dob", "valid_from", "expiry_date",
                                  "subscription_start_date", "contract_end_date"])
def test_attribute_dates_are_not_relevant(name):
    assert not is_retention_relevant(tcol(name, "date", temporal=True))


def test_non_temporal_is_not_relevant():
    assert not is_retention_relevant(tcol("email", "text"))


# --- policy resolution ---------------------------------------------- #

def test_policy_resolution_order():
    p = RetentionPolicy(default_days=365, tables={"orders": 2555},
                        columns={"orders.notes": 90})
    assert p.days_for("orders", "notes") == (90, "column")
    assert p.days_for("orders", "total") == (2555, "table")
    assert p.days_for("users", "email") == (365, "default")


def test_policy_from_yaml(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("default_days: 200\ntables:\n  orders: 1000\nexclude:\n  - '*.updated_at'\n")
    p = RetentionPolicy.from_file(f)
    assert p.default_days == 200
    assert p.days_for("orders")[0] == 1000
    assert p.is_excluded("orders", "updated_at")


# --- score maths ---------------------------------------------------- #

def test_age_score_curve():
    assert _age_score(300, 365)[0] == 0.0            # not yet due
    assert _age_score(365, 365)[0] == 0.0            # exactly at policy
    assert _age_score(365 + 182, 365)[0] == pytest.approx(0.5, abs=0.01)   # half a period over
    assert _age_score(365 * 2, 365)[0] == 1.0        # a full period over -> capped
    assert _age_score(365 * 5, 365)[0] == 1.0
    assert _age_score(None, 365)[0] == 0.0


# --- synthetic table ---------------------------------------------- #

def test_old_table_flags_all_columns_recent_does_not():
    old = one_table_db([
        tcol("id", "integer"),
        tcol("created_at", "timestamptz", temporal=True, min_iso=iso(900), max_iso=iso(1)),
        tcol("email", "text"),
        tcol("date_of_birth", "date", temporal=True, min_iso="1955-01-01", max_iso="2005-01-01"),
    ], name="acct")
    res = RetentionChecker(RetentionPolicy(default_days=365), old, now=NOW).check()

    assert res.table("acct").is_overdue
    assert res.column("acct.email").is_overdue          # inherits
    assert res.column("acct.email").basis == "table:created_at"
    assert res.column("acct.created_at").basis == "self"
    # date_of_birth must inherit the row age, NOT be scored on its 1955 value
    dob = res.column("acct.date_of_birth")
    assert dob.basis == "table:created_at"
    assert dob.score == res.table("acct").score

    fresh = one_table_db([
        tcol("id", "integer"),
        tcol("created_at", "timestamptz", temporal=True, min_iso=iso(120), max_iso=iso(1)),
        tcol("email", "text"),
    ], name="acct")
    res2 = RetentionChecker(RetentionPolicy(default_days=365), fresh, now=NOW).check()
    assert not res2.table("acct").is_overdue
    assert res2.column("acct.email").score == 0.0


def test_excluded_column_never_scores():
    db = one_table_db([
        tcol("created_at", "timestamptz", temporal=True, min_iso=iso(900)),
        tcol("updated_at", "timestamptz", temporal=True, min_iso=iso(900)),
    ], name="acct")
    p = RetentionPolicy(default_days=365, exclude=["*.updated_at"])
    res = RetentionChecker(p, db, now=NOW).check()
    assert res.column("acct.updated_at").score == 0.0
    assert res.column("acct.updated_at").basis == "excluded"
    assert res.column("acct.created_at").score == 1.0


def test_policy_longer_than_data_clears_everything():
    db = one_table_db([
        tcol("created_at", "timestamptz", temporal=True, min_iso=iso(900), max_iso=iso(1)),
        tcol("x", "text"),
    ], name="acct")
    res = RetentionChecker(RetentionPolicy(default_days=3650), db, now=NOW).check()
    assert not res.table("acct").is_overdue
    assert res.column("acct.x").score == 0.0


def test_output_shape_parallels_the_other_signals():
    db = one_table_db([tcol("created_at", "timestamptz", temporal=True, min_iso=iso(900))],
                      name="acct")
    c = RetentionChecker(RetentionPolicy(), db, now=NOW).check().column("acct.created_at")
    d = c.to_dict()
    assert set(d) >= {"qualified_name", "score", "is_overdue", "days_overdue",
                      "policy_applied", "oldest_row_age_days", "basis"}
    assert 0.0 <= d["score"] <= 1.0


# --- against the live seed --------------------------------------- #

def test_seed_tables_overdue_at_one_year(metadata):
    res = RetentionChecker(RetentionPolicy(default_days=365), metadata).check()
    for name in ("users", "orders", "support_tickets"):
        t = res.table(name)
        assert t.is_overdue, name
        assert t.score >= 0.9
        assert t.oldest_row_age_days > 400


def test_seed_tables_clear_with_a_long_policy(metadata):
    res = RetentionChecker(RetentionPolicy(default_days=3650), metadata).check()
    for name in ("users", "orders", "support_tickets"):
        assert not res.table(name).is_overdue
    assert all(c.score == 0.0 for c in res.columns)


def test_date_of_birth_inherits_not_self(metadata):
    res = RetentionChecker(RetentionPolicy(default_days=365), metadata).check()
    dob = res.column("users.date_of_birth")
    assert dob.basis == "table:created_at"
    assert dob.score == res.table("users").score


def test_mothers_maiden_name_is_the_spec_example(metadata):
    # "mothers_maiden_name ... 400 days overdue" -> inherits the stale user row
    res = RetentionChecker(RetentionPolicy(default_days=365), metadata).check()
    mmn = res.column("users.mothers_maiden_name")
    assert mmn.is_overdue
    assert mmn.days_overdue > 400
    assert mmn.score >= 0.9


def test_example_policy_discriminates(metadata):
    from pathlib import Path
    policy = RetentionPolicy.from_file(Path("retention_policy.example.yaml"))
    res = RetentionChecker(policy, metadata).check()
    assert res.table("support_tickets").is_overdue      # 365d policy, 800+d data
    assert not res.table("orders").is_overdue           # 2555d policy
    assert not res.table("users").is_overdue            # 1095d policy
    assert res.column("orders.updated_at").basis == "excluded"


def test_live_fraction_overdue_matches_seeding(connector, metadata):
    if not connector.report.ok:
        pytest.skip("needs read-only connection")
    res = RetentionChecker(RetentionPolicy(default_days=365), metadata,
                           connector=connector).check()
    users = res.table("users")
    assert users.fraction_overdue is not None
    assert 0.10 < users.fraction_overdue < 0.90
    assert users.rows_overdue and users.rows_overdue > 50
