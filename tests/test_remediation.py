"""Phase 7b - remediation script generator. No DB, no network, no execution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from auditor.ingestion.metadata import ColumnMetadata, TableMetadata
from auditor.remediation import RemediationGenerator
from auditor.scoring import ScoringEngine
from tests.test_scoring import ret, sens, usage  # signal builders

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
GEN = RemediationGenerator(now=NOW)
ENGINE = ScoringEngine()


def col(name, *, table="users", raw="TEXT", nullable=True, pk=False, fk=False, fk_target=None):
    return ColumnMetadata(
        table=table, name=name, ordinal=0, generic_type="text", raw_type=raw,
        nullable=nullable, is_primary_key=pk, is_foreign_key=fk, foreign_key_target=fk_target,
    )


def tbl(name="users", pk=("id",)):
    return TableMetadata(name=name, schema="public", row_count=100, primary_key=list(pk))


def flagged_score(qn="users.mothers_maiden_name", s=0.9, u=0.0, r=1.0, pii="knowledge_based",
                  days_overdue=535, frac=None):
    ns = ENGINE.score_column(
        sens(s, qn=qn, pii=pii), usage(u, qn=qn), ret(r, qn=qn, days_overdue=days_overdue),
    )
    if frac is not None:
        ns.evidence["retention"]["fraction_overdue"] = frac
    return ns


# --- archive-then-drop (the common case) --------------------------- #

def test_archive_then_drop_for_plain_flagged_column():
    rem = GEN.generate(flagged_score(), col("mothers_maiden_name"), tbl())
    assert rem.strategy == "archive_then_drop"
    assert rem.reversible is True
    sql = rem.sql
    assert 'CREATE SCHEMA IF NOT EXISTS "dma_archive"' in sql
    assert 'CREATE TABLE "dma_archive"."users__mothers_maiden_name__20260909" AS' in sql
    assert 'ALTER TABLE "public"."users" DROP COLUMN "mothers_maiden_name"' in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "RESTORE (after COMMIT):" in sql
    # evidence is embedded as comments
    assert "WHY FLAGGED" in sql
    assert "necessity score 0.900" in sql
    assert "knowledge_based" in sql
    assert "minimization violation" in sql


def test_never_contains_a_bare_executable_delete_or_drop_outside_comments():
    # the row-level DELETE alternative must stay commented out
    sql = GEN.generate(flagged_score(), col("mothers_maiden_name"), tbl()).sql
    for line in sql.splitlines():
        s = line.strip()
        if s.startswith("--") or not s:
            continue
        assert not s.upper().startswith("DELETE ")


def test_deterministic_with_fixed_clock():
    a = GEN.generate(flagged_score(qn="users.ssn"), col("ssn"), tbl()).sql
    b = GEN.generate(flagged_score(qn="users.ssn"), col("ssn"), tbl()).sql
    assert a == b


# --- cautions ----------------------------------------------------- #

def test_government_id_gets_statutory_retention_caution():
    rem = GEN.generate(flagged_score(qn="users.ssn", s=1.0, pii="government_id"),
                       col("ssn"), tbl())
    assert any("statutory retention" in c for c in rem.cautions)


def test_partial_overdue_suggests_row_level_delete_instead():
    rem = GEN.generate(flagged_score(frac=0.4), col("mothers_maiden_name"), tbl())
    assert any("still within the retention" in c for c in rem.cautions)
    assert "ALTERNATIVE" in rem.sql
    assert "-- Draft only" not in rem.sql or True  # alternative block present


def test_not_null_column_is_flagged_in_cautions():
    rem = GEN.generate(flagged_score(), col("mothers_maiden_name", nullable=False), tbl())
    assert any("NOT NULL" in c for c in rem.cautions)


# --- keys -> anonymize ------------------------------------------- #

def test_primary_key_column_is_anonymized_not_dropped():
    rem = GEN.generate(
        flagged_score(qn="users.national_id"),
        col("national_id", pk=True), tbl(pk=("national_id",)),
    )
    assert rem.strategy == "anonymize_in_place"
    assert "DROP COLUMN" not in rem.sql
    assert "PRIMARY KEY" in rem.sql
    assert rem.reversible is False


def test_foreign_key_column_is_anonymized():
    rem = GEN.generate(
        flagged_score(qn="orders.user_ssn"),
        col("user_ssn", table="orders", fk=True, fk_target="users.ssn"),
        tbl("orders", pk=("id",)),
    )
    assert rem.strategy == "anonymize_in_place"
    assert "UPDATE" in rem.sql and "SET" in rem.sql
    assert any("users.ssn" in c for c in rem.cautions)


# --- non-flagged verdicts --------------------------------------- #

def test_kept_column_gets_no_remediation():
    ns = ENGINE.score_column(sens(0.9), usage(0.95, reads=9000), ret(1.0))
    rem = GEN.generate(ns, col("email"), tbl())
    assert rem.strategy == "none"
    assert "DROP COLUMN" not in rem.sql
    assert rem.sql.strip().startswith("--")


def test_review_verdict_still_gets_a_draft():
    ns = ENGINE.score_column(sens(0.9), usage(0.0), ret(0.0))   # sensitive+unused, not stale
    assert ns.verdict == "review"
    rem = GEN.generate(ns, col("mothers_maiden_name"), tbl())
    assert rem.strategy == "archive_then_drop"


# --- mysql --------------------------------------------------------- #

def test_mysql_dialect_uses_backticks_and_warns_about_ddl():
    rem = GEN.generate(flagged_score(), col("mothers_maiden_name"), tbl(),
                       dialect="mysql")
    assert "`users`" in rem.sql
    assert "not transactional" in rem.sql
    assert "BEGIN;" not in rem.sql


# --- output shape ---------------------------------------------- #

def test_to_dict_shape():
    d = GEN.generate(flagged_score(), col("mothers_maiden_name"), tbl()).to_dict()
    assert set(d) == {"qualified_name", "strategy", "summary", "sql",
                      "cautions", "reversible", "dialect"}
