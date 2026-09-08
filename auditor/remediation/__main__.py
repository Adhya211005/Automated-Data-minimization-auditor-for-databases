"""CLI for the remediation script generator.

    python -m auditor.remediation                      draft fixes for every flagged column
    python -m auditor.remediation --all                ... include 'review' columns too
    python -m auditor.remediation --out drafts/         write one .sql file per column
    python -m auditor.remediation --policy p.yaml --metadata m.json --log q.jsonl

Draft only - this never connects to a database and never runs SQL.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from auditor.remediation.generator import RemediationGenerator
from auditor.scoring.pipeline import AuditRunConfig, run_audit


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auditor.remediation", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metadata")
    ap.add_argument("--log", default=AuditRunConfig().log_path)
    ap.add_argument("--policy")
    ap.add_argument("--default-days", type=int)
    ap.add_argument("--window", type=int, default=90)
    ap.add_argument("--all", action="store_true", help="also draft for 'review' columns")
    ap.add_argument("--out", metavar="DIR", help="write one .sql file per column")
    args = ap.parse_args(argv)

    run = run_audit(AuditRunConfig(
        metadata_path=args.metadata, log_path=args.log, policy_path=args.policy,
        policy_days=args.default_days, window_days=args.window,
    ))
    gen = RemediationGenerator()

    verdicts = {"flag_for_deletion"} | ({"review"} if args.all else set())
    targets = [ns for ns in run.result.columns if ns.verdict in verdicts]

    if not targets:
        print("Nothing to remediate.", file=sys.stderr)
        return 0

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for ns in targets:
        rem = gen.generate(
            ns, run.metadata.column(ns.qualified_name), run.metadata.table(ns.table),
        )
        if out_dir:
            fp = out_dir / f"{ns.table}__{ns.column}.sql"
            fp.write_text(rem.sql, encoding="utf-8")
            print(f"wrote {fp}  ({rem.strategy})")
        else:
            print(rem.sql)
            print()

    if out_dir:
        print(f"\n{len(targets)} draft(s) in {out_dir}/ - review before running any of them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
