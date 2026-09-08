"""CLI for the sensitivity classifier.

    python -m auditor.sensitivity                     classify the live target DB, print a table
    python -m auditor.sensitivity --metadata m.json   classify from a saved metadata dump (no DB)
    python -m auditor.sensitivity --json              emit ColumnSensitivity records as JSON
    python -m auditor.sensitivity --evaluate          score against seed-data/output/column_catalog.csv
"""

from __future__ import annotations

import argparse
import json
import sys

from auditor.ingestion.metadata import DatabaseMetadata
from auditor.sensitivity.classifier import RegexSensitivityClassifier
from auditor.sensitivity.evaluation import (
    DEFAULT_GROUND_TRUTH,
    evaluate,
    load_ground_truth,
)


def _load_metadata(path: str | None) -> DatabaseMetadata:
    if path:
        return DatabaseMetadata.load(path)
    from auditor.ingestion import extract_metadata

    return extract_metadata()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auditor.sensitivity", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metadata", metavar="FILE", help="metadata JSON from `python -m auditor.ingestion --out`")
    ap.add_argument("--json", action="store_true", help="print ColumnSensitivity records as JSON")
    ap.add_argument("--evaluate", action="store_true", help="score against the ground-truth catalogue")
    ap.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH))
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args(argv)

    meta = _load_metadata(args.metadata)
    clf = RegexSensitivityClassifier(threshold=args.threshold)
    preds = clf.classify_all(meta.iter_columns())

    if args.evaluate:
        report = evaluate(preds, load_ground_truth(args.ground_truth))
        print(report.format())
        return 0 if (report.precision >= 0.8 and report.recall >= 0.8) else 1

    if args.json:
        print(json.dumps([p.to_dict() for p in preds], indent=2))
        return 0

    print(f"{'column':<36} {'score':>6} {'tier':<8} {'sensitive':<10} {'pii_type':<16} rules")
    print("-" * 100)
    for p in sorted(preds, key=lambda x: (-x.score, x.qualified_name)):
        rules = ", ".join(r for r in p.matched_rules if not r.startswith("harmless"))[:40]
        print(f"{p.qualified_name:<36} {p.score:>6.2f} {p.tier:<8} "
              f"{'yes' if p.is_sensitive else 'no':<10} {str(p.pii_type or ''):<16} {rules}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
