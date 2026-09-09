"""ML sensitivity classifier + hybrid. The regex baseline is unchanged and
still covered by tests/test_sensitivity.py."""

from __future__ import annotations

import warnings

import pytest

from auditor.ingestion.metadata import ColumnMetadata
from auditor.sensitivity.classifier import RegexSensitivityClassifier
from auditor.sensitivity.features import ColumnFeaturizer, engineered_features
from auditor.sensitivity.ml_classifier import (
    HybridSensitivityClassifier,
    MLSensitivityClassifier,
    load_model,
    save_model,
    train_model,
)
from auditor.sensitivity.synth import generate_synthetic_columns

warnings.filterwarnings("ignore")


def col(name, *, gtype="text", raw="TEXT", samples=(), nullable=True, temporal=False):
    return ColumnMetadata(
        table="t", name=name, ordinal=0, generic_type=gtype, raw_type=raw,
        nullable=nullable, is_temporal=temporal,
        sample_values=list(samples), sample_size=len(samples),
        distinct_in_sample=len(set(samples)),
    )


@pytest.fixture(scope="module")
def model():
    # one deterministic model for the whole module
    return train_model(seed=42)


@pytest.fixture(scope="module")
def ml(model):
    return MLSensitivityClassifier(model=model)


# --- features ----------------------------------------------------- #

def test_engineered_features_capture_format_signatures():
    f = engineered_features(col("attr", samples=["a@b.com", "c@d.org", "e@f.net"]))
    assert f["sig_email"] == 1.0
    assert f["type_text"] == 1.0 and f["type_integer"] == 0.0
    assert f["val_words_mean"] == 1.0

    g = engineered_features(col("x", gtype="timestamptz", temporal=True,
                                samples=["2024-01-01T00:00:00+00:00"]))
    assert g["is_temporal"] == 1.0
    assert g["sig_iso_date"] == 1.0


def test_featurizer_shape_and_names():
    cols = [col("email", samples=["a@b.com"]), col("created_at", gtype="timestamptz")]
    fz = ColumnFeaturizer()
    X = fz.fit_transform(cols)
    assert X.shape[0] == 2
    assert any(n.startswith("ngram::") for n in fz.feature_names_)
    assert any(n == "eng::sig_email" for n in fz.feature_names_)


def test_usage_feature_toggle_changes_dimension():
    c = [col("email", samples=["a@b.com"])]
    with_u = ColumnFeaturizer(include_usage=True).fit_transform(c).shape[1]
    without_u = ColumnFeaturizer(include_usage=False).fit_transform(c).shape[1]
    assert with_u == without_u + 4


# --- synthetic corpus ------------------------------------------- #

def test_synth_is_deterministic_and_varied():
    a = generate_synthetic_columns(120, seed=1)
    b = generate_synthetic_columns(120, seed=1)
    assert [c.name for c in a] == [c.name for c in b]
    assert len(a) == 120

    tiers = {RegexSensitivityClassifier().classify(c).tier for c in a}
    assert tiers == {"none", "low", "medium", "high"}   # all four represented


def test_synth_excludes_the_real_seed_columns():
    names = {c.name for c in generate_synthetic_columns(900)}
    # the seed's giveaway column is never synthesised verbatim as-is with table
    assert not any(c.table == "users" for c in generate_synthetic_columns(50))


# --- ML classifier -------------------------------------------- #

def test_ml_output_is_columnsensitivity_shaped(ml):
    r = ml.classify(col("email", samples=["a@b.com", "x@y.com"]))
    d = r.to_dict()
    assert set(d) >= {"qualified_name", "score", "is_sensitive", "tier", "pii_type",
                      "confidence", "matched_rules", "signals"}
    assert 0.0 <= r.score <= 1.0
    assert r.matched_rules == ["ml:logreg"]
    assert "tier_proba" in r.signals


@pytest.mark.parametrize("name,samples,sensitive", [
    ("email_address", ["jane.doe@corp.com", "bob@x.io"], True),
    ("home_address", ["12 Oak St, Springfield"], True),
    ("ssn", ["123-45-6789", "222-33-4444"], True),
    ("created_at", ["2024-05-01T10:00:00+00:00"], False),
    ("favorite_color", ["blue", "green", "red"], False),
    ("status", ["active", "closed"], False),
])
def test_ml_gets_obvious_cases_right(ml, name, samples, sensitive):
    assert ml.classify(col(name, samples=samples)).is_sensitive is sensitive


def test_model_round_trips(model, tmp_path):
    p = tmp_path / "m.joblib"
    save_model(model, p)
    reloaded = load_model(p, train_if_missing=False)
    c = col("phone", samples=["4155551234", "3105559876"])
    a = MLSensitivityClassifier(model=model).classify(c)
    b = MLSensitivityClassifier(model=reloaded).classify(c)
    assert (a.score, a.tier) == (b.score, b.tier)


# --- hybrid --------------------------------------------------- #

def test_hybrid_default_mode_is_blend():
    h = HybridSensitivityClassifier()
    assert h.mode == "blend" and h.regex_weight == 0.6


def test_hybrid_blends_and_flags_disagreement(model):
    h = HybridSensitivityClassifier(model=model)
    # last_login_at: regex zeros it (timestamp), ML sees mild activity PII
    r = h.classify(col("last_login_at", gtype="timestamptz", temporal=True,
                       samples=["2024-01-01T00:00:00+00:00"]))
    assert "ml:logreg" in r.matched_rules
    assert set(r.signals) >= {"regex", "ml", "disagreement", "mode"}
    if r.signals["disagreement"]:
        assert "needs_review:regex_ml_disagreement" in r.matched_rules


def test_hybrid_modes_use_the_right_source(model):
    c = col("email", samples=["a@b.com", "c@d.com"])
    reg = RegexSensitivityClassifier().classify(c)
    ml = MLSensitivityClassifier(model=model).classify(c)
    assert HybridSensitivityClassifier(model=model, mode="regex_primary").classify(c).score == reg.score
    assert HybridSensitivityClassifier(model=model, mode="ml_primary").classify(c).score == ml.score
    blended = HybridSensitivityClassifier(model=model, mode="blend").classify(c).score
    assert min(reg.score, ml.score) - 0.01 <= blended <= max(reg.score, ml.score) + 0.01


# --- side-by-side comparison on the seed (needs DB) ------------ #

def test_ml_and_hybrid_hold_up_against_regex_on_the_seed(metadata, model):
    from auditor.sensitivity.evaluation_ml import compare

    c = compare(metadata, model=model)

    assert c.seed_regex.precision == 1.0 and c.seed_regex.recall == 1.0     # baseline unchanged
    assert c.synthetic_ml["sensitive_f1"] >= 0.95                            # learned the rules

    # the ML approximation should not badly underperform the baseline
    assert c.seed_ml.precision >= 0.95
    assert c.seed_ml.recall >= 0.90
    # hybrid must not regress below the ML model, and ideally matches regex
    assert c.seed_hybrid.f1 >= c.seed_ml.f1
    assert c.seed_hybrid.recall >= 0.95
    assert c.seed_hybrid.false_positives == []

    # disagreements are reported, not hidden
    assert isinstance(c.disagreements, list)
