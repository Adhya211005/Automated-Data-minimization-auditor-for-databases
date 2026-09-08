"""Phase 2 - regex sensitivity baseline.

Unit tests build synthetic ColumnMetadata so the classifier is testable with
no database (CLAUDE.md). The evaluation test uses the live seed metadata and
asserts precision/recall against seed-data/output/column_catalog.csv.
"""

from __future__ import annotations

import pytest

from auditor.ingestion.metadata import ColumnMetadata
from auditor.sensitivity import (
    RegexSensitivityClassifier,
    classify_metadata,
    evaluate,
    load_ground_truth,
)


def col(name, *, table="t", gtype="text", raw="TEXT", samples=(), nullable=True,
        pk=False, fk=False, fk_target=None, unique=False, temporal=False) -> ColumnMetadata:
    return ColumnMetadata(
        table=table, name=name, ordinal=0, generic_type=gtype, raw_type=raw,
        nullable=nullable, is_primary_key=pk, is_foreign_key=fk,
        foreign_key_target=fk_target, is_unique=unique, is_temporal=temporal,
        sample_values=list(samples), sample_size=len(samples),
    )


CLF = RegexSensitivityClassifier()


# --- names alone --------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "email", "email_address", "phone", "mobile_number", "ssn", "social_security_number",
    "mothers_maiden_name", "password", "password_hash", "date_of_birth", "home_address",
    "shipping_address", "credit_card_number",
])
def test_obvious_sensitive_names_flagged(name):
    r = CLF.classify(col(name))
    assert r.is_sensitive, r
    assert r.score >= 0.5


@pytest.mark.parametrize("name", [
    "id", "created_at", "updated_at", "status", "is_active", "favorite_color",
    "theme_preference", "locale", "currency", "priority", "category",
    "total_amount", "satisfaction_rating", "customer_sentiment", "promo_code",
])
def test_obvious_harmless_names_not_flagged(name):
    r = CLF.classify(col(name, temporal=name.endswith("_at")))
    assert not r.is_sensitive, r
    assert r.score < 0.5


# --- the cases the spec calls out -------------------------------------- #

def test_last_ip_is_sensitive_despite_harmless_sounding_name():
    # "a column named last_ip sounds harmless but is legally sensitive"
    r = CLF.classify(col("last_login_ip", gtype="inet", raw="INET",
                         samples=["156.34.97.0", "122.86.142.177", "8.8.8.8"]))
    assert r.is_sensitive
    assert r.pii_type == "network_id"


def test_favorite_color_is_not_a_compliance_risk():
    r = CLF.classify(col("favorite_color", samples=["blue", "green", "red"]))
    assert not r.is_sensitive
    assert r.tier == "none"


def test_ssn_detected_from_values_even_with_opaque_name():
    r = CLF.classify(col("attr_7", samples=["405-56-9919", "143-84-8786", "222-11-0000"]))
    assert r.is_sensitive
    assert r.pii_type == "government_id"
    assert any("value:" in m for m in r.matched_rules)


def test_email_in_freetext_column_raises_score():
    r = CLF.classify(col("body", samples=[
        "please contact me at john.doe@example.com about my order",
        "reset link sent to jane@corp.co.uk",
        "my number is 555-234-5566 call me",
    ]))
    assert r.is_sensitive


# --- structural signals ----------------------------------------------- #

def test_primary_key_forced_to_zero():
    assert CLF.classify(col("id", gtype="integer", pk=True)).score == 0.0


def test_unmatched_foreign_key_is_linkage_low():
    r = CLF.classify(col("user_id", gtype="integer", fk=True, fk_target="users.id"))
    assert not r.is_sensitive
    assert r.pii_type == "linkage"
    assert 0 < r.score <= 0.15


def test_date_of_birth_beats_the_temporal_harmless_signal():
    r = CLF.classify(col("date_of_birth", gtype="date", raw="DATE", temporal=True,
                         samples=["1952-09-27", "1997-02-19"]))
    assert r.is_sensitive
    assert r.tier in {"medium", "high"}


# --- output contract ------------------------------------------------- #

def test_output_shape():
    r = CLF.classify(col("email", samples=["a@b.com"]))
    d = r.to_dict()
    assert set(d) >= {"qualified_name", "score", "is_sensitive", "tier", "pii_type",
                      "confidence", "matched_rules", "signals"}
    assert 0.0 <= d["score"] <= 1.0
    assert d["tier"] in {"none", "low", "medium", "high"}


# --- evaluation against the seed ground truth (needs DB) --------------- #

def test_precision_recall_against_seed_ground_truth(metadata):
    preds = classify_metadata(metadata)
    report = evaluate(preds, load_ground_truth())

    assert report.n == 46
    assert report.precision >= 0.9, report.format()
    assert report.recall >= 0.9, report.format()
    assert report.false_negatives == [], f"missed sensitive columns: {report.false_negatives}"
    assert report.false_positives == [], f"over-flagged: {report.false_positives}"
    # score should track the ground-truth magnitude, not just the yes/no
    assert report.score_mae <= 0.15


def test_specific_seed_columns(metadata):
    by_name = {p.qualified_name: p for p in classify_metadata(metadata)}
    assert by_name["users.email"].tier == "high"
    assert by_name["users.mothers_maiden_name"].is_sensitive
    assert by_name["users.mothers_maiden_name"].pii_type == "knowledge_based"
    assert not by_name["users.favorite_color"].is_sensitive
    assert by_name["orders.ip_address"].is_sensitive
    assert not by_name["orders.card_brand"].is_sensitive
