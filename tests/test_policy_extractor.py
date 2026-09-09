"""NLP extraction of a RetentionPolicy from a policy document.

The point of the end-to-end test: a policy built by the extractor must drive
the Phase 4 RetentionChecker *identically* to a hand-written policy with the
same numbers - the extractor is a new way to make the config, not a new engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from auditor.retention.checker import RetentionChecker
from auditor.retention.policy import RetentionPolicy
from auditor.retention.policy_extractor import (
    SAMPLE_DOC,
    PolicyDraft,
    _to_days,
    extract_policy,
    extract_rules,
    propose_mappings,
)

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DOC_TEXT = SAMPLE_DOC.read_text(encoding="utf-8")


# --- duration parsing ------------------------------------------------ #

@pytest.mark.parametrize("num,unit,days", [
    ("7", "year", 2555), ("1", "year", 365), ("18", "month", 540),
    ("6", "month", 180), ("90", "day", 90), ("two", "year", 730),
    ("a", "year", 365), ("three", "month", 90),
])
def test_to_days(num, unit, days):
    assert _to_days(num, unit) == days


# --- rule extraction from the sample doc ---------------------------- #

def test_extracts_the_expected_periods():
    days = sorted(r.retention_days for r in extract_rules(DOC_TEXT))
    # 7y orders, 6mo survey, 1y tickets, 18mo demo, 2y marketing, 2y default,
    # 90d auth, 3y users, 3y withdrawal
    assert days == [90, 180, 365, 540, 730, 730, 1095, 1095, 2555]


def test_default_rule_is_flagged():
    defaults = [r for r in extract_rules(DOC_TEXT) if r.is_default]
    assert len(defaults) == 1 and defaults[0].retention_days == 730


def test_ignores_non_retention_durations():
    """§3 of the doc: DSAR window, breach window, training deadline, backup
    cycle, review cadence - all have numbers, none are retention periods."""
    text = "\n".join(extract_rules(DOC_TEXT)[i].sentence for i in range(len(extract_rules(DOC_TEXT))))
    for forbidden in ("access request", "72 hours", "breach", "training",
                      "rotated", "reviewed every"):
        assert forbidden.lower() not in text.lower()


def test_handles_a_sentence_wrapped_across_lines():
    wrapped = ("Order records, including invoices and\n"
               "payment details, must be retained for\n"
               "7 years for tax purposes.")
    rules = extract_rules(wrapped)
    assert len(rules) == 1
    assert rules[0].retention_days == 2555
    assert "order" in rules[0].keywords and "invoice" in rules[0].keywords


def test_conditional_phrasing_lowers_confidence():
    a = extract_rules("Marketing data is retained for 2 years.")[0]
    b = extract_rules("Marketing data is retained for 2 years unless renewed.")[0]
    assert b.confidence < a.confidence


def test_synthetic_doc_is_reproducible():
    assert SAMPLE_DOC.exists()
    assert extract_rules(DOC_TEXT) == extract_rules(SAMPLE_DOC.read_text(encoding="utf-8"))


# --- mapping (needs the seed metadata) ----------------------------- #

def test_categories_map_to_the_right_schema_objects(metadata):
    props = {p.rule.retention_days: p for p in
             propose_mappings(extract_rules(DOC_TEXT), metadata)}
    assert props[2555].target == "orders"
    assert props[365].target == "support_tickets"
    assert props[1095 if 1095 in props else 1095].target in ("users", "users.marketing_consent")
    assert props[540].target in ("users.gender", "users.date_of_birth")
    assert props[730].target_kind == "default"


def test_every_proposal_needs_confirmation(metadata):
    for p in propose_mappings(extract_rules(DOC_TEXT), metadata):
        assert p.needs_confirmation is True


def test_conflicts_are_surfaced(metadata):
    draft = extract_policy(SAMPLE_DOC, metadata)
    targets = {c["target"] for c in draft.conflicts}
    assert "users.marketing_consent" in targets     # 2y vs 3y
    assert "support_tickets" in targets             # 1y ticket vs 6mo survey


# --- the end-to-end equivalence test ----------------------------- #

def test_drafted_policy_drives_the_checker_identically_to_a_handwritten_one(metadata):
    draft = extract_policy(SAMPLE_DOC, metadata)
    drafted = draft.to_policy(accept={"orders", "support_tickets", "users",
                                      "users.marketing_consent", "users.gender"})

    # a hand-written policy with the SAME numbers the draft landed on
    handwritten = RetentionPolicy(
        default_days=730,
        tables={"orders": 2555, "support_tickets": 365, "users": 1095},
        columns={"users.marketing_consent": 1095, "users.gender": 540},
    )
    assert drafted.to_dict() == handwritten.to_dict()

    a = RetentionChecker(drafted, metadata, now=NOW).check()
    b = RetentionChecker(handwritten, metadata, now=NOW).check()

    assert {c.qualified_name for c in a.columns} == {c.qualified_name for c in b.columns}
    for qn in a.by_name:
        ca, cb = a.by_name[qn], b.by_name[qn]
        assert (ca.score, ca.is_overdue, ca.days_overdue, ca.basis) == \
               (cb.score, cb.is_overdue, cb.days_overdue, cb.basis), qn
    for name in ("users", "orders", "support_tickets"):
        ta, tb = a.table(name), b.table(name)
        assert (ta.score, ta.is_overdue, ta.policy_days) == (tb.score, tb.is_overdue, tb.policy_days)


def test_checker_module_is_untouched():
    """The extractor must not need any change to the Phase 4 engine."""
    import auditor.retention.checker as checker
    import auditor.retention.policy_extractor as extractor
    src = Path(extractor.__file__).read_text(encoding="utf-8")
    # it consumes RetentionPolicy / RetentionChecker, never redefines them
    assert "class RetentionChecker" not in src
    assert "class RetentionPolicy" not in src
    assert hasattr(checker, "RetentionChecker")


def test_review_yaml_round_trips(metadata, tmp_path):
    draft = extract_policy(SAMPLE_DOC, metadata)
    f = tmp_path / "draft.yaml"
    f.write_text(draft.to_review_yaml(), encoding="utf-8")
    loaded = RetentionPolicy.from_file(f)
    assert loaded.default_days == 730
    assert loaded.tables.get("orders") == 2555
