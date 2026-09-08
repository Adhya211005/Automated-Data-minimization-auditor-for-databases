"""Phase 5 - Necessity scoring engine.

The edge-case tests build the three input signals by hand (no DB, no other
phase) - that is the isolation CLAUDE.md asks for. The end-to-end test runs
the real pipeline against the seed and checks the four spec anchors.
"""

from __future__ import annotations

import pytest

from auditor.retention.checker import ColumnRetention
from auditor.scoring import ScoringEngine, THRESHOLDS
from auditor.sensitivity.classifier import ColumnSensitivity
from auditor.usage.analyzer import ColumnUsage

ENGINE = ScoringEngine()


# --- builders -------------------------------------------------------- #

def sens(score, qn="users.c", pii="contact"):
    return ColumnSensitivity(
        qualified_name=qn, table=qn.split(".")[0], column=qn.split(".")[-1],
        score=score, is_sensitive=score >= 0.5, tier="high" if score >= 0.7 else "low",
        pii_type=pii, confidence=0.9, matched_rules=["name:test"], signals={},
    )


def usage(score, qn="users.c", reads=0):
    return ColumnUsage(
        qualified_name=qn, table=qn.split(".")[0], column=qn.split(".")[-1],
        score=score, is_used=score > 0, tier="heavy" if score > 0.75 else "unused",
        read_count=reads, write_count=0, total_count=reads,
        last_read_at=None, last_write_at=None, last_access_at=None, first_access_at=None,
        distinct_days=0, distinct_services=0,
        access_by_type={"read": reads, "write": 0}, access_by_service={},
        query_templates=[], window_days=90,
    )


def ret(score, qn="users.c", days_overdue=None):
    return ColumnRetention(
        qualified_name=qn, table=qn.split(".")[0], column=qn.split(".")[-1],
        score=score, is_overdue=score > 0, days_overdue=days_overdue,
        policy_applied={"days": 365, "source": "default", "anchor": "created_at"},
        oldest_row_age_days=900 if score > 0 else 100,
        newest_row_age_days=1, basis="table:created_at",
    )


# --- the four spec edge cases -------------------------------------- #

def test_sensitive_and_used_is_not_flagged_regardless_of_staleness():
    # email: high sensitivity, very high usage, and stale -> KEEP
    ns = ENGINE.score_column(sens(0.90), usage(0.95, reads=10_000), ret(1.0, days_overdue=500))
    assert ns.is_flagged is False
    assert ns.verdict == "keep"
    assert ns.is_unused is False


def test_sensitive_unused_but_fresh_is_not_flagged():
    # collected sensitive data nobody queries, but the rows aren't overdue yet
    ns = ENGINE.score_column(sens(0.90), usage(0.0), ret(0.0))
    assert ns.is_flagged is False
    assert ns.verdict == "review"
    assert (ns.is_sensitive, ns.is_unused, ns.is_stale) == (True, True, False)


def test_sensitive_unused_and_stale_is_flagged():
    # mothers_maiden_name
    ns = ENGINE.score_column(sens(0.90, pii="knowledge_based"), usage(0.0),
                             ret(1.0, days_overdue=535))
    assert ns.is_flagged is True
    assert ns.verdict == "flag_for_deletion"
    assert ns.score == pytest.approx(0.90)


def test_harmless_unused_and_stale_is_not_flagged():
    # favorite_color - unused and overdue, but not sensitive enough to matter
    ns = ENGINE.score_column(sens(0.05), usage(0.0), ret(1.0, days_overdue=535))
    assert ns.is_flagged is False
    assert ns.verdict == "not_a_risk"
    assert ns.score == pytest.approx(0.05)


# --- thresholds are explicit cuts on the 0-1 scores --------------- #

def test_threshold_boundaries():
    t = THRESHOLDS
    # exactly at the sensitive cut -> counts as sensitive (>=)
    assert ENGINE.score_column(sens(t["sensitive"]), usage(0.0), ret(1.0)).is_flagged
    assert not ENGINE.score_column(sens(t["sensitive"] - 0.01), usage(0.0), ret(1.0)).is_flagged
    # exactly at the unused cut -> counts as unused (<=)
    assert ENGINE.score_column(sens(0.9), usage(t["unused"]), ret(1.0)).is_flagged
    assert not ENGINE.score_column(sens(0.9), usage(t["unused"] + 0.01), ret(1.0)).is_flagged
    # exactly at the stale cut -> counts as stale (>=)
    assert ENGINE.score_column(sens(0.9), usage(0.0), ret(t["stale"])).is_flagged
    assert not ENGINE.score_column(sens(0.9), usage(0.0), ret(t["stale"] - 0.01)).is_flagged


def test_score_is_the_documented_product():
    ns = ENGINE.score_column(sens(0.8), usage(0.25), ret(0.6))
    assert ns.score == pytest.approx(0.8 * (1 - 0.25) * 0.6, abs=1e-4)


def test_flag_needs_all_three_not_just_a_big_product():
    # sensitivity 0.9, usage 0.30 (just above the cut), staleness 1.0
    # product = 0.9 * 0.70 * 1.0 = 0.63  (large) but is_unused is False
    ns = ENGINE.score_column(sens(0.9), usage(0.30), ret(1.0))
    assert ns.score > 0.6
    assert ns.is_flagged is False
    assert ns.verdict == "keep"


def test_missing_signals_default_to_neutral():
    ns = ENGINE.score_column(sens(0.9), None, None, qualified_name="users.c")
    assert ns.usage == 0.0 and ns.retention == 0.0
    assert ns.is_flagged is False        # nothing stale -> not flagged
    assert ns.score == 0.0


def test_breakdown_and_reasons_present():
    ns = ENGINE.score_column(sens(0.9, pii="knowledge_based"), usage(0.0), ret(1.0, days_overdue=535))
    b = ns.breakdown
    assert b["formula"] == "sensitivity * (1 - usage) * staleness"
    assert b["sensitivity"]["is_sensitive"] and b["usage"]["is_unused"] and b["staleness"]["is_stale"]
    assert b["usage"]["factor"] == pytest.approx(1.0)
    assert any("minimization violation" in r for r in ns.reasons)
    assert ns.evidence["sensitivity"]["pii_type"] == "knowledge_based"


# --- end to end against the seed ---------------------------------- #

@pytest.fixture(scope="module")
def scored(metadata):
    from auditor.retention.checker import RetentionChecker
    from auditor.retention.policy import RetentionPolicy
    from auditor.sensitivity.classifier import RegexSensitivityClassifier
    from auditor.usage.analyzer import UsageAnalyzer

    s = RegexSensitivityClassifier().classify_all(metadata.iter_columns())
    u = UsageAnalyzer(metadata, window_days=90, mode="hybrid").analyze(
        "seed-data/output/query_log.jsonl").columns
    r = RetentionChecker(RetentionPolicy(default_days=365), metadata).check().columns
    return ScoringEngine().score_all(s, u, r)


def test_four_spec_anchors_land_correctly(scored):
    assert scored.column("users.email").verdict == "keep"
    assert scored.column("users.email").is_flagged is False

    mmn = scored.column("users.mothers_maiden_name")
    assert mmn.verdict == "flag_for_deletion" and mmn.is_flagged

    fav = scored.column("users.favorite_color")
    assert fav.verdict == "not_a_risk" and fav.is_flagged is False

    ssn = scored.column("users.ssn")           # spec 1.5: "necessary-looking but never queried"
    assert ssn.verdict == "flag_for_deletion" and ssn.is_flagged


def test_flagged_set_is_small_and_sensible(scored):
    flagged = {c.qualified_name for c in scored.flagged}
    assert flagged == {"users.mothers_maiden_name", "users.ssn"}
    # both sit at the top of the ranking
    assert [c.qualified_name for c in scored.columns[:2]] == \
           ["users.mothers_maiden_name", "users.ssn"]


def test_actively_used_sensitive_columns_are_not_flagged(scored):
    for qn in ("users.email", "users.password_hash", "users.phone", "users.full_name",
               "orders.shipping_address", "orders.card_last4"):
        c = scored.column(qn)
        assert not c.is_flagged, f"{qn} should not be flagged (it's queried)"


def test_review_verdict_appears_with_a_longer_policy(metadata):
    from auditor.retention.checker import RetentionChecker
    from auditor.retention.policy import RetentionPolicy
    from auditor.sensitivity.classifier import RegexSensitivityClassifier
    from auditor.usage.analyzer import UsageAnalyzer

    s = RegexSensitivityClassifier().classify_all(metadata.iter_columns())
    u = UsageAnalyzer(metadata, window_days=90, mode="hybrid").analyze(
        "seed-data/output/query_log.jsonl").columns
    # policy so long that nothing is stale -> sensitive+unused columns become "review"
    r = RetentionChecker(RetentionPolicy(default_days=5000), metadata).check().columns
    res = ScoringEngine().score_all(s, u, r)
    assert res.n_flagged == 0
    mmn = res.column("users.mothers_maiden_name")
    assert mmn.verdict == "review" and not mmn.is_flagged
