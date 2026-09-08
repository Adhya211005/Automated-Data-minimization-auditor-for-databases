"""CLI for the ingestion layer.

    python -m auditor.ingestion --check           verify the connection is read-only, exit 0/1
    python -m auditor.ingestion                   extract metadata, print a summary
    python -m auditor.ingestion --json            print the full metadata document as JSON
    python -m auditor.ingestion --out meta.json   write the JSON document to a file
    python -m auditor.ingestion --tables users,orders --sample-rows 50
"""

from __future__ import annotations

import argparse
import sys

from auditor.config import TargetConfig
from auditor.ingestion.connector import ReadOnlyConnector, ReadOnlyViolation
from auditor.ingestion.metadata import MetadataExtractor


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auditor.ingestion", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="only run the read-only verification and print the report")
    ap.add_argument("--json", action="store_true", help="print full metadata JSON to stdout")
    ap.add_argument("--out", metavar="FILE", help="write metadata JSON to FILE")
    ap.add_argument("--tables", help="comma-separated subset of tables to extract")
    ap.add_argument("--sample-rows", type=int, default=30,
                    help="rows to sample per table for value inspection (default 30)")
    ap.add_argument("--max-samples", type=int, default=12,
                    help="distinct sample values to keep per column (default 12)")
    ap.add_argument("--allow-writable", action="store_true",
                    help="do NOT abort if the connection turns out to be writable (dangerous)")
    args = ap.parse_args(argv)

    cfg = TargetConfig.from_env()
    print(f"target: {cfg.safe_repr()}", file=sys.stderr)

    try:
        connector = ReadOnlyConnector(cfg, strict=not args.allow_writable)
    except ReadOnlyViolation as exc:
        print(f"\nREFUSING TO PROCEED:\n{exc}", file=sys.stderr)
        return 2

    print("\n" + connector.report.summary(), file=sys.stderr)

    if args.check:
        return 0 if connector.report.ok else 1

    if not connector.report.ok:
        print("\nconnection is not read-only - not extracting (use --check to inspect)", file=sys.stderr)
        return 1

    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    meta = MetadataExtractor(connector).extract(
        tables=tables, sample_rows=args.sample_rows, max_samples=args.max_samples
    )
    connector.dispose()

    if args.out:
        path = meta.save(args.out)
        print(f"\nwrote {path}  ({meta.to_dict()['table_count']} tables, "
              f"{meta.to_dict()['column_count']} columns)", file=sys.stderr)
    if args.json:
        print(meta.to_json())
    if not args.json and not args.out:
        print("\n" + meta.summary_table())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
