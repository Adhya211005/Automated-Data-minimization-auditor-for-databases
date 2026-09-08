"""Phase 3 - usage analyzer + scoring.

Synthetic-log tests exercise the scoring in isolation (no DB). The evaluation
tests run against the live seed metadata + Phase 0 query log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auditor.usage import UsageAnalyzer
from auditor.usage.evaluation import (
    evaluate_parser,
    evaluate_usage,
    load_usage_ground_truth,
    log_derived_ground_truth,
)
from auditor.usage.logsource import LogEntry

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def entry(days_ago, op, reads=(), writes=(), sql="", service="app", template="t"):
    return LogEntry(
        ts=NOW - timedelta(days=days_ago), query_id="q", operation=op, sql=sql,
        service=service, template=template,
        declared_reads=list(reads), declared_writes=list(writes),
    )


# --- scoring in isolation (declared mode, no DB) ----------------------- #

def test_never_touched_column_scores_zero():
    log = [entry(1, "SELECT", reads=["users.email"]) for _ in range(50)]
    res = UsageAnalyzer(window_days=90, now=NOW, mode="declared").analyze(log)
    # a column that never appears simply isn't in the result when no metadata
    assert res.column("users.mothers_maiden_name") is None
    email = res.column("users.email")
    assert email.is_used and email.score > 0.5


def test_frequent_recent_column_scores_near_one():
    log = [entry(d % 90, "SELECT", reads=["users.email", "users.id"]) for d in range(4000)]
    log += [entry(d % 90, "SELECT", reads=["users.ssn"]) for d in range(3)]
    res = UsageAnalyzer(window_days=90, now=NOW, mode="declared").analyze(log)
    assert res.column("users.email").score >= 0.85
    assert res.column("users.ssn").score < 0.4
    assert res.column("users.email").score > res.column("users.ssn").score


def test_recency_pulls_down_a_column_that_went_quiet():
    fresh = [entry(1, "SELECT", reads=["t.fresh"]) for _ in range(200)]
    stale = [entry(85, "SELECT", reads=["t.stale"]) for _ in range(200)]
    res = UsageAnalyzer(window_days=90, now=NOW, mode="declared").analyze(fresh + stale)
    assert res.column("t.fresh").score > res.column("t.stale").score


def test_window_excludes_old_queries():
    log = [entry(200, "SELECT", reads=["t.old"])] + [entry(2, "SELECT", reads=["t.new"])]
    res = UsageAnalyzer(window_days=90, now=NOW, mode="declared").analyze(log)
    assert res.n_in_window == 1
    assert res.column("t.old") is None


def test_read_write_split_recorded():
    log = [entry(1, "UPDATE", writes=["t.c"]) for _ in range(10)]
    log += [entry(1, "SELECT", reads=["t.c"]) for _ in range(3)]
    res = UsageAnalyzer(window_days=90, now=NOW, mode="declared").analyze(log)
    c = res.column("t.c")
    assert c.read_count == 3 and c.write_count == 10
    assert c.access_by_type == {"read": 3, "write": 10}


def test_output_shape_parallels_sensitivity():
    log = [entry(1, "SELECT", reads=["t.c"], service="web")]
    c = UsageAnalyzer(window_days=90, now=NOW, mode="declared").analyze(log).column("t.c")
    d = c.to_dict()
    assert set(d) >= {"qualified_name", "score", "is_used", "tier", "read_count",
                      "write_count", "last_access_at", "access_by_type",
                      "access_by_service", "window_days"}
    assert 0.0 <= d["score"] <= 1.0
    assert d["tier"] in {"unused", "rare", "occasional", "frequent", "heavy"}


# --- against the seed (needs DB + Phase 0 log) ------------------------- #

SEED_LOG = "seed-data/output/query_log.jsonl"


@pytest.fixture(scope="module")
def usage_result(metadata):
    return UsageAnalyzer(metadata, window_days=90, mode="hybrid").analyze(SEED_LOG)


def test_every_metadata_column_gets_a_row(metadata, usage_result):
    assert {c.qualified_name for c in usage_result.columns} == set(metadata.column_names)


def test_never_queried_columns_score_zero(usage_result):
    for qn in ("users.mothers_maiden_name", "users.favorite_color", "orders.notes",
               "support_tickets.customer_sentiment", "support_tickets.satisfaction_rating"):
        c = usage_result.column(qn)
        assert c.score == 0.0 and c.is_used is False, qn


def test_frequently_queried_columns_score_high(usage_result):
    assert usage_result.column("users.email").score >= 0.85
    assert usage_result.column("users.id").score >= 0.85


def test_rarely_queried_sensitive_column_is_low_but_nonzero(usage_result):
    ssn = usage_result.column("users.ssn")
    assert 0.0 < ssn.score < 0.35
    assert ssn.read_count < 20


def test_binary_used_unused_is_perfect(usage_result):
    ev = evaluate_usage(usage_result, load_usage_ground_truth())
    assert ev.used_precision == 1.0
    assert ev.used_recall == 1.0
    assert ev.false_negatives == []


def test_score_tracks_real_log_frequency(usage_result):
    ev = evaluate_usage(usage_result)
    # objective ground truth (buckets of actual log frequency)
    assert ev.spearman_log_derived >= 0.9, ev.format()


def test_parser_fidelity_on_seed_log(metadata):
    from auditor.usage.parser import QueryParser
    pe = evaluate_parser(SEED_LOG, QueryParser(metadata))
    assert pe.n_unparseable == 0
    assert pe.precision >= 0.9
    assert pe.recall >= 0.85
