"""Side-by-side evaluation: ML classifier vs the regex baseline.

Two distinct measurements:

  1. held-out SYNTHETIC columns - ML predicting the regex tier it was trained
     on. This says "did the model learn the rules". Regex is trivially 100%
     here (the labels are its own output), so only the ML number is reported.

  2. the 46 SEED columns vs the hand-authored column_catalog.csv tiers -
     an INDEPENDENT label set neither classifier was trained on. This is the
     real comparison. Regex scored precision/recall 1.0 here in Phase 2;
     this shows whether the ML approximation holds up on real columns.

Plus: every seed column where regex and ML disagree, with the catalog's
answer, so you can see who is right.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auditor.ingestion.metadata import DatabaseMetadata
from auditor.sensitivity.classifier import RegexSensitivityClassifier
from auditor.sensitivity.evaluation import EvalReport, evaluate, load_ground_truth
from auditor.sensitivity.ml_classifier import (
    TIERS,
    HybridSensitivityClassifier,
    MLSensitivityClassifier,
    TrainedModel,
    train_model,
)

_TIER_ORD = {t: i for i, t in enumerate(TIERS)}


@dataclass
class Comparison:
    synthetic_ml: dict                       # from TrainedModel.train_report
    seed_regex: EvalReport
    seed_ml: EvalReport
    seed_hybrid: EvalReport
    disagreements: list[dict] = field(default_factory=list)
    model_type: str = ""
    include_usage: bool = False

    def format(self) -> str:
        out = []
        out.append("held-out SYNTHETIC (ML approximating the regex oracle):")
        s = self.synthetic_ml
        out.append(f"  n={s['n_test']}  tier_acc={s['tier_accuracy']:.3f}  "
                   f"P={s['sensitive_precision']:.3f}  R={s['sensitive_recall']:.3f}  "
                   f"F1={s['sensitive_f1']:.3f}")
        out.append("")
        out.append(f"SEED (46 cols) vs column_catalog.csv  -  model={self.model_type}, "
                   f"usage_feature={self.include_usage}")
        out.append(f"  {'':<10} {'precision':>10} {'recall':>10} {'F1':>8} "
                   f"{'tier_acc':>9} {'score_MAE':>10}")
        for name, rep in (("regex", self.seed_regex), ("ml", self.seed_ml),
                          ("hybrid", self.seed_hybrid)):
            out.append(f"  {name:<10} {rep.precision:>10.3f} {rep.recall:>10.3f} "
                       f"{rep.f1:>8.3f} {rep.tier_accuracy:>9.3f} {rep.score_mae:>10.3f}")
        if self.seed_ml.false_negatives:
            out.append(f"  ml false negatives : {self.seed_ml.false_negatives}")
        if self.seed_ml.false_positives:
            out.append(f"  ml false positives : {self.seed_ml.false_positives}")
        out.append("")
        if self.disagreements:
            out.append(f"regex vs ML disagreements on the seed ({len(self.disagreements)}):")
            out.append(f"  {'column':<34} {'regex':>14} {'ml':>14} {'catalog':>9}  who")
            for d in self.disagreements:
                out.append(f"  {d['column']:<34} "
                           f"{d['regex_tier']+' '+format(d['regex_score'],'.2f'):>14} "
                           f"{d['ml_tier']+' '+format(d['ml_score'],'.2f'):>14} "
                           f"{d['catalog_tier']:>9}  {d['closer']}")
        else:
            out.append("regex and ML agree on all 46 seed columns.")
        return "\n".join(out)


def _disagreements(metadata, regex_preds, ml_preds, gt) -> list[dict]:
    rx = {p.qualified_name: p for p in regex_preds}
    ml = {p.qualified_name: p for p in ml_preds}
    rows = []
    for qn in rx:
        r, m = rx[qn], ml[qn]
        if r.tier == m.tier and r.is_sensitive == m.is_sensitive:
            continue
        g = gt.get(qn)
        cat_tier = g.tier if g else "?"
        cat_ord = _TIER_ORD.get(cat_tier)
        closer = "-"
        if cat_ord is not None:
            dr = abs(_TIER_ORD[r.tier] - cat_ord)
            dm = abs(_TIER_ORD[m.tier] - cat_ord)
            closer = "regex" if dr < dm else "ml" if dm < dr else "tie"
        rows.append({
            "column": qn,
            "regex_tier": r.tier, "regex_score": r.score,
            "ml_tier": m.tier, "ml_score": m.score,
            "catalog_tier": cat_tier, "closer": closer,
        })
    return rows


def compare(
    metadata: DatabaseMetadata,
    *,
    model: TrainedModel | None = None,
    model_type: str = "logreg",
    include_usage: bool = False,
    usages: dict | None = None,
) -> Comparison:
    model = model or train_model(model_type=model_type, include_usage=include_usage)
    gt = load_ground_truth()
    cols = list(metadata.iter_columns())

    regex_preds = RegexSensitivityClassifier().classify_all(cols)
    ml_preds = MLSensitivityClassifier(model=model).classify_all(cols, usages)
    hybrid_preds = HybridSensitivityClassifier(model=model).classify_all(cols, usages)

    return Comparison(
        synthetic_ml=model.train_report,
        seed_regex=evaluate(regex_preds, gt),
        seed_ml=evaluate(ml_preds, gt),
        seed_hybrid=evaluate(hybrid_preds, gt),
        disagreements=_disagreements(metadata, regex_preds, ml_preds, gt),
        model_type=model.model_type,
        include_usage=model.include_usage,
    )


def compare_usage_variants(metadata: DatabaseMetadata, *, usages: dict | None = None) -> str:
    """Train with and without the usage feature; report both."""
    lines = []
    for iu in (False, True):
        c = compare(metadata, include_usage=iu, usages=usages if iu else None)
        lines.append(f"=== usage_feature = {iu} ===")
        lines.append(c.format())
        lines.append("")
    return "\n".join(lines)
