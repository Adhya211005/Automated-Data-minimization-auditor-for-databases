"""Score the usage analyzer against the Phase 0 seed.

Two independent checks:

1. evaluate_parser() - does the SQL parser recover the columns each query
   touches? Compared against the ``columns_read`` / ``columns_written`` the
   Phase 0 log declares for every entry. Reported as micro precision/recall
   over distinct SQL statements, with the disagreements listed - the seed's
   declared lists and the sketch SQL do not match perfectly, and this shows
   exactly where.

2. evaluate_usage() - do the per-column Usage scores line up with the
   ``expected_usage`` labels in ``column_catalog.csv``
   (never < rare < low < medium < high)? Reported as:
     * binary "is used" precision/recall (never -> unused)
     * Spearman rank correlation of score vs the ordinal label
     * the score for the spec's anchor columns (mothers_maiden_name ~ 0,
       email ~ 1)
     * columns where the label and the observed traffic disagree
"""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from auditor.usage.analyzer import UsageResult
from auditor.usage.logsource import read_query_log
from auditor.usage.parser import QueryParser

DEFAULT_GROUND_TRUTH = (
    Path(__file__).resolve().parents[2] / "seed-data" / "output" / "column_catalog.csv"
)
DEFAULT_LOG = (
    Path(__file__).resolve().parents[2] / "seed-data" / "output" / "query_log.jsonl"
)

USAGE_ORDINAL = {"never": 0, "rare": 1, "low": 2, "medium": 3, "high": 4}


# --------------------------------------------------------------------------- #
# 1. parser fidelity
# --------------------------------------------------------------------------- #

@dataclass
class ParserEval:
    n_statements: int
    n_parsed_sqlglot: int
    n_insert_regex: int
    n_unparseable: int
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int
    discrepancies: list[dict] = field(default_factory=list)

    def format(self) -> str:
        out = [
            f"distinct SQL statements : {self.n_statements}",
            f"  parsed by sqlglot     : {self.n_parsed_sqlglot}",
            f"  insert-regex fallback : {self.n_insert_regex}",
            f"  unparseable           : {self.n_unparseable}",
            f"column attribution vs the log's declared columns (micro):",
            f"  precision : {self.precision:.3f}   (parsed cols that are in the declared set)",
            f"  recall    : {self.recall:.3f}   (declared cols the parser recovered)",
            f"  f1        : {self.f1:.3f}   TP={self.tp} FP={self.fp} FN={self.fn}",
        ]
        if self.discrepancies:
            out.append("\n  per-statement differences:")
            for d in self.discrepancies:
                out.append(f"    {d['template'] or d['sql'][:40]}")
                if d["parser_missed"]:
                    out.append(f"        parser missed  : {d['parser_missed']}")
                if d["parser_extra"]:
                    out.append(f"        parser found + : {d['parser_extra']}")
        return "\n".join(out)


def evaluate_parser(
    log: str | Path | Iterable = DEFAULT_LOG,
    parser: QueryParser | None = None,
) -> ParserEval:
    parser = parser or QueryParser()
    entries = read_query_log(log) if isinstance(log, (str, Path)) else log

    distinct: "OrderedDict[str, dict]" = OrderedDict()
    for e in entries:
        if not e.sql or e.sql in distinct:
            continue
        distinct[e.sql] = {
            "template": e.template,
            "declared": set(e.declared_reads) | set(e.declared_writes),
        }

    tp = fp = fn = 0
    n_sqlglot = n_regex = n_bad = 0
    discrepancies: list[dict] = []

    for sql, info in distinct.items():
        qc = parser.parse(sql)
        got = qc.all_columns
        declared = info["declared"]
        if qc.method == "sqlglot":
            n_sqlglot += 1
        elif qc.method == "insert-regex":
            n_regex += 1
        else:
            n_bad += 1

        hit = got & declared
        missed = declared - got
        extra = got - declared
        tp += len(hit)
        fn += len(missed)
        fp += len(extra)
        if missed or extra:
            discrepancies.append({
                "template": info["template"],
                "sql": sql,
                "parser_missed": sorted(missed),
                "parser_extra": sorted(extra),
            })

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return ParserEval(
        n_statements=len(distinct), n_parsed_sqlglot=n_sqlglot,
        n_insert_regex=n_regex, n_unparseable=n_bad,
        precision=precision, recall=recall, f1=f1, tp=tp, fp=fp, fn=fn,
        discrepancies=discrepancies,
    )


# --------------------------------------------------------------------------- #
# 2. usage score vs the catalogue
# --------------------------------------------------------------------------- #

def load_usage_ground_truth(path: str | Path = DEFAULT_GROUND_TRUTH) -> dict[str, str]:
    """The hand-authored ``expected_usage`` labels from the Phase 0 catalogue.
    These encode *design intent* and don't perfectly match the realised query
    log - see ``log_derived_ground_truth`` for the objective version."""
    out: dict[str, str] = {}
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[f"{row['table']}.{row['column']}"] = row["expected_usage"].strip()
    return out


def log_derived_ground_truth(
    log: str | Path | Iterable = DEFAULT_LOG,
) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    """Objective ground truth: bucket every column by how often the query log's
    own declared ``columns_read``/``columns_written`` touch it.

    never  = 0 accesses
    rare/low/medium/high = quartiles of (reads + 0.2*writes) among active columns

    Returns (label_by_column, (reads, writes)_by_column).
    """
    entries = read_query_log(log) if isinstance(log, (str, Path)) else log
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    for e in entries:
        for c in e.declared_reads:
            reads[c] = reads.get(c, 0) + 1
        for c in e.declared_writes:
            writes[c] = writes.get(c, 0) + 1

    cols = set(reads) | set(writes)
    weighted = {c: reads.get(c, 0) + 0.2 * writes.get(c, 0) for c in cols}
    active = sorted(w for w in weighted.values() if w > 0)

    def band(w: float) -> str:
        if w <= 0:
            return "never"
        # position among active columns
        rank = sum(1 for a in active if a <= w) / len(active)
        if rank <= 0.25:
            return "rare"
        if rank <= 0.50:
            return "low"
        if rank <= 0.75:
            return "medium"
        return "high"

    labels = {c: band(weighted[c]) for c in cols}
    counts = {c: (reads.get(c, 0), writes.get(c, 0)) for c in cols}
    return labels, counts


@dataclass
class UsageEval:
    n: int
    used_precision: float
    used_recall: float
    used_tp: int
    used_fp: int
    used_tn: int
    used_fn: int
    spearman: float                 # vs the hand-authored catalogue label
    spearman_log_derived: float     # vs the objective log-derived label
    anchors: dict = field(default_factory=dict)
    band_means: dict = field(default_factory=dict)
    disagreements: list[dict] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)

    def format(self) -> str:
        out = [
            f"columns evaluated        : {self.n}",
            "",
            "binary  used vs unused  (ground truth: expected_usage == 'never' -> unused)",
            f"  confusion : TP={self.used_tp} FP={self.used_fp} TN={self.used_tn} FN={self.used_fn}",
            f"  precision : {self.used_precision:.3f}",
            f"  recall    : {self.used_recall:.3f}",
        ]
        if self.false_positives:
            out.append(f"  false positives (scored used, labelled never): {self.false_positives}")
        if self.false_negatives:
            out.append(f"  false negatives (scored unused, labelled used): {self.false_negatives}")
        out += [
            "",
            f"ordinal   Spearman(score, catalogue label)    : {self.spearman:.3f}   (design-intent label)",
            f"          Spearman(score, log-derived label)  : {self.spearman_log_derived:.3f}   (objective - buckets of real log frequency)",
            "",
            "anchor columns (spec):",
        ]
        for name, val in self.anchors.items():
            out.append(f"  {name:<34} score = {val:.3f}")
        out.append("\nmean predicted score by expected_usage label (should increase):")
        for band in ("never", "rare", "low", "medium", "high"):
            if band in self.band_means:
                m = self.band_means[band]
                out.append(f"  {band:<8} {m['mean']:.3f}   (n={m['n']})")
        if self.disagreements:
            out.append("\ncatalogue label vs observed traffic - disagreements (>= 2 bands):")
            out.append("  (these are Phase 0 labels written from design intent; the query-log")
            out.append("   templates read/write these columns more than the label implies -")
            out.append("   the analyzer is reporting the actual traffic, correctly)")
            for d in self.disagreements:
                out.append(f"  {d['column']:<34} label={d['label']:<7} "
                           f"score={d['score']:.3f} tier={d['tier']:<11} "
                           f"reads={d['reads']} writes={d['writes']}")
        return "\n".join(out)


def _spearman(pairs: list[tuple[float, float]]) -> float:
    if len(pairs) < 3:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rx = _rankdata(xs)
    ry = _rankdata(ys)
    n = len(pairs)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


def _rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def evaluate_usage(
    result: UsageResult,
    ground_truth: dict[str, str] | None = None,
    log_derived: dict[str, str] | None = None,
) -> UsageEval:
    gt = ground_truth or load_usage_ground_truth()
    if log_derived is None:
        log_derived, _ = log_derived_ground_truth()
    by_name = result.by_name
    common = [qn for qn in gt if qn in by_name]

    tp = fp = tn = fn = 0
    fps: list[str] = []
    fns: list[str] = []
    pairs: list[tuple[float, float]] = []
    bands: dict[str, list[float]] = {}
    disagreements: list[dict] = []

    for qn in common:
        label = gt[qn]
        cu = by_name[qn]
        gt_used = label != "never"
        if cu.is_used and gt_used:
            tp += 1
        elif cu.is_used and not gt_used:
            fp += 1; fps.append(qn)
        elif not cu.is_used and gt_used:
            fn += 1; fns.append(qn)
        else:
            tn += 1

        if label in USAGE_ORDINAL:
            pairs.append((cu.score, USAGE_ORDINAL[label]))
            bands.setdefault(label, []).append(cu.score)

        # tier -> ordinal for the disagreement check
        tier_ord = {"unused": 0, "rare": 1, "occasional": 2, "frequent": 3, "heavy": 4}[cu.tier]
        if label in USAGE_ORDINAL and abs(tier_ord - USAGE_ORDINAL[label]) >= 2:
            disagreements.append({
                "column": qn, "label": label, "score": cu.score, "tier": cu.tier,
                "reads": cu.read_count, "writes": cu.write_count,
            })

    anchors = {}
    for a in ("users.mothers_maiden_name", "users.email", "users.ssn",
              "users.favorite_color", "orders.notes"):
        if a in by_name:
            anchors[a] = by_name[a].score

    band_means = {
        b: {"mean": sum(v) / len(v), "n": len(v)} for b, v in bands.items()
    }

    log_pairs = [
        (by_name[qn].score, USAGE_ORDINAL[log_derived[qn]])
        for qn in common
        if qn in log_derived and log_derived[qn] in USAGE_ORDINAL
    ]

    return UsageEval(
        n=len(common),
        used_precision=tp / (tp + fp) if (tp + fp) else 0.0,
        used_recall=tp / (tp + fn) if (tp + fn) else 0.0,
        used_tp=tp, used_fp=fp, used_tn=tn, used_fn=fn,
        spearman=_spearman(pairs),
        spearman_log_derived=_spearman(log_pairs),
        anchors=anchors, band_means=band_means,
        disagreements=disagreements, false_positives=fps, false_negatives=fns,
    )
