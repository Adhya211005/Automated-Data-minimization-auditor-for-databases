"""Score the regex baseline against a ground-truth column catalogue.

Ground truth is ``seed-data/output/column_catalog.csv`` from Phase 0. Its
``sensitivity_tier`` column is the label: a column is "truly sensitive" when
its tier is ``high`` or ``medium`` (that is also where ``sensitivity_score``
>= ~0.5). Everything ``low``/``none`` is "truly harmless".

Primary metric is precision/recall of the binary sensitive/not decision.
Secondary: 4-tier accuracy and mean absolute error of the 0-1 score.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from auditor.sensitivity.classifier import ColumnSensitivity

DEFAULT_GROUND_TRUTH = (
    Path(__file__).resolve().parents[2] / "seed-data" / "output" / "column_catalog.csv"
)
POSITIVE_TIERS = ("high", "medium")


@dataclass(frozen=True)
class GroundTruthColumn:
    qualified_name: str
    table: str
    column: str
    tier: str
    score: float
    pii_type: str
    expected_usage: str
    seeded_stale: bool

    @property
    def is_sensitive(self) -> bool:
        return self.tier in POSITIVE_TIERS


def load_ground_truth(path: str | Path = DEFAULT_GROUND_TRUTH) -> dict[str, GroundTruthColumn]:
    path = Path(path)
    out: dict[str, GroundTruthColumn] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            qn = f"{row['table']}.{row['column']}"
            out[qn] = GroundTruthColumn(
                qualified_name=qn,
                table=row["table"],
                column=row["column"],
                tier=row["sensitivity_tier"].strip(),
                score=float(row["sensitivity_score"]),
                pii_type=row["pii_type"].strip(),
                expected_usage=row["expected_usage"].strip(),
                seeded_stale=row["seeded_stale_rows"].strip().lower() == "true",
            )
    return out


@dataclass
class EvalReport:
    n: int
    tp: int
    fp: int
    tn: int
    fn: int
    precision: float
    recall: float
    f1: float
    accuracy: float
    tier_accuracy: float
    score_mae: float
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    def format(self, show_rows: bool = True) -> str:
        out = []
        if show_rows:
            out.append(f"{'column':<36} {'pred':>6} {'tier':<7} {'truth':>6} {'tier':<7}  result")
            out.append("-" * 78)
            for r in sorted(self.rows, key=lambda x: (-x["pred_score"], x["column"])):
                mark = {"TP": "ok", "TN": "ok", "FP": "FALSE POSITIVE", "FN": "FALSE NEGATIVE"}[r["result"]]
                out.append(
                    f"{r['column']:<36} {r['pred_score']:>6.2f} {r['pred_tier']:<7} "
                    f"{r['truth_score']:>6.2f} {r['truth_tier']:<7}  {mark}"
                )
            out.append("")
        out += [
            f"columns evaluated : {self.n}",
            f"confusion         : TP={self.tp}  FP={self.fp}  TN={self.tn}  FN={self.fn}",
            f"precision         : {self.precision:.3f}",
            f"recall            : {self.recall:.3f}",
            f"F1                : {self.f1:.3f}",
            f"accuracy          : {self.accuracy:.3f}",
            f"tier accuracy     : {self.tier_accuracy:.3f}  (4-class: none/low/medium/high)",
            f"score MAE         : {self.score_mae:.3f}  (vs ground-truth 0-1 score)",
        ]
        if self.false_positives:
            out.append(f"false positives   : {', '.join(self.false_positives)}")
        if self.false_negatives:
            out.append(f"false negatives   : {', '.join(self.false_negatives)}")
        return "\n".join(out)


def evaluate(
    predictions: Iterable[ColumnSensitivity],
    ground_truth: dict[str, GroundTruthColumn],
) -> EvalReport:
    preds = {p.qualified_name: p for p in predictions}
    common = [qn for qn in preds if qn in ground_truth]

    tp = fp = tn = fn = 0
    tier_ok = 0
    abs_err = 0.0
    rows: list[dict] = []
    fps: list[str] = []
    fns: list[str] = []

    for qn in common:
        p, g = preds[qn], ground_truth[qn]
        if p.is_sensitive and g.is_sensitive:
            result = "TP"; tp += 1
        elif p.is_sensitive and not g.is_sensitive:
            result = "FP"; fp += 1; fps.append(qn)
        elif not p.is_sensitive and g.is_sensitive:
            result = "FN"; fn += 1; fns.append(qn)
        else:
            result = "TN"; tn += 1

        tier_ok += int(p.tier == g.tier)
        abs_err += abs(p.score - g.score)
        rows.append({
            "column": qn,
            "pred_score": p.score, "pred_tier": p.tier,
            "truth_score": g.score, "truth_tier": g.tier,
            "result": result,
        })

    n = len(common)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy = (tp + tn) / n if n else 0.0

    return EvalReport(
        n=n, tp=tp, fp=fp, tn=tn, fn=fn,
        precision=precision, recall=recall, f1=f1, accuracy=accuracy,
        tier_accuracy=tier_ok / n if n else 0.0,
        score_mae=abs_err / n if n else 0.0,
        false_positives=fps, false_negatives=fns, rows=rows,
    )
