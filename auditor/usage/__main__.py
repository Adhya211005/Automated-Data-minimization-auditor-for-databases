"""CLI for the usage analyzer.

    python -m auditor.usage                       analyze the seed log, print per-column table
    python -m auditor.usage --json                emit ColumnUsage records as JSON
    python -m auditor.usage --evaluate            score vs the Phase 0 ground truth (both checks)
    python -m auditor.usage --evaluate-parser     just the SQL-parser fidelity check
    python -m auditor.usage --log FILE --metadata FILE --window 90 --mode hybrid
"""

from __future__ import annotations

import argparse
import json
import sys

from auditor.usage.analyzer import UsageAnalyzer
from auditor.usage.evaluation import (
    DEFAULT_GROUND_TRUTH,
    DEFAULT_LOG,
    evaluate_parser,
    evaluate_usage,
    load_usage_ground_truth,
)
from auditor.usage.parser import QueryParser


def _load_metadata(path: str | None):
    if path:
        from auditor.ingestion.metadata import DatabaseMetadata
        return DatabaseMetadata.load(path)
    from auditor.ingestion import extract_metadata
    return extract_metadata()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auditor.usage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--metadata", metavar="FILE", help="metadata JSON; omit to extract from the live DB")
    ap.add_argument("--window", type=int, default=90, help="window in days (default 90)")
    ap.add_argument("--mode", choices=["sql", "declared", "hybrid"], default="hybrid")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--evaluate-parser", action="store_true")
    ap.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH))
    ap.add_argument("--no-metadata", action="store_true",
                    help="don't touch the DB; only columns seen in the log get a row")
    args = ap.parse_args(argv)

    if args.evaluate_parser and not (args.evaluate or args.json):
        report = evaluate_parser(args.log, QueryParser(
            None if args.no_metadata else _load_metadata(args.metadata)))
        print(report.format())
        return 0 if report.recall >= 0.6 else 1

    meta = None if args.no_metadata else _load_metadata(args.metadata)
    analyzer = UsageAnalyzer(meta, window_days=args.window, mode=args.mode)
    result = analyzer.analyze(args.log)

    if args.evaluate:
        print("=== SQL parser fidelity ===")
        pe = evaluate_parser(args.log, QueryParser(meta))
        print(pe.format())
        print("\n=== Usage score vs ground truth ===")
        ue = evaluate_usage(result, load_usage_ground_truth(args.ground_truth))
        print(ue.format())
        ok = (ue.used_recall >= 0.99 and ue.used_precision >= 0.99
              and ue.spearman_log_derived >= 0.85
              and ue.anchors.get("users.mothers_maiden_name", 1) <= 0.05
              and ue.anchors.get("users.email", 0) >= 0.85)
        return 0 if ok else 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print(f"window: {result.window_start[:10]} .. {result.window_end[:10]}  "
          f"({result.n_in_window:,} of {result.n_queries:,} queries in window)")
    print(f"attribution: {result.n_parsed_sql:,} parsed / {result.n_fallback_declared:,} "
          f"declared-fallback / {result.n_unparseable:,} unparseable  [mode={result.mode}]\n")
    print(result.summary_table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
