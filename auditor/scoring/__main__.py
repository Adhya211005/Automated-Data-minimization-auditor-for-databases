"""End-to-end audit: ingestion -> sensitivity + usage + retention -> Necessity Score.

    python -m auditor.scoring                        full pipeline on the live seed DB
    python -m auditor.scoring --metadata m.json --log query_log.jsonl   offline
    python -m auditor.scoring --policy retention_policy.example.yaml
    python -m auditor.scoring --top 15
    python -m auditor.scoring --flagged-only
    python -m auditor.scoring --json
    python -m auditor.scoring --evaluate            check the four spec anchors
"""

from __future__ import annotations

import argparse
import json
import sys

from auditor.scoring.pipeline import DEFAULT_LOG, AuditRunConfig, run_audit


def run_pipeline(args):
    run = run_audit(AuditRunConfig(
        metadata_path=args.metadata,
        log_path=args.log,
        policy_path=args.policy,
        policy_days=args.default_days,
        window_days=args.window,
        mode=args.mode,
        live_retention=args.live_retention,
    ))
    return run.result, run.policy


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auditor.scoring", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metadata", metavar="FILE")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--policy", metavar="FILE")
    ap.add_argument("--default-days", type=int)
    ap.add_argument("--window", type=int, default=90)
    ap.add_argument("--mode", choices=["sql", "declared", "hybrid"], default="hybrid")
    ap.add_argument("--live-retention", action="store_true",
                    help="query row-fraction-past-policy (otherwise metadata min/max only)")
    ap.add_argument("--top", type=int, help="show only the top N rows")
    ap.add_argument("--flagged-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args(argv)

    result, policy = run_pipeline(args)

    if args.evaluate:
        return _evaluate(result)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    t = result.thresholds
    print(f"thresholds: sensitive >= {t['sensitive']}   unused <= {t['unused']}   "
          f"stale >= {t['stale']}    policy default = {policy.default_days}d\n")

    if args.flagged_only:
        flagged = result.flagged
        if not flagged:
            print("nothing flagged.")
            return 0
        for i, c in enumerate(flagged, 1):
            print(f"{i}. {c.qualified_name}   necessity={c.score:.3f}")
            for r in c.reasons:
                print(f"     {r}")
        return 0

    print(result.ranked_report(top=args.top))
    return 0


def _evaluate(result) -> int:
    """The four cases from CLAUDE.md / the spec §4 worked example."""
    expect = {
        "users.email":               ("keep",              False),
        "users.mothers_maiden_name": ("flag_for_deletion", True),
        "users.favorite_color":      ("not_a_risk",        False),
        "users.ssn":                 ("flag_for_deletion", True),   # bonus: spec §1.5
    }
    ok = True
    print(f"{'column':<30} {'necessity':>9}  {'verdict':<18} {'flagged':<8} expected")
    print("-" * 78)
    for qn, (want_verdict, want_flag) in expect.items():
        c = result.column(qn)
        if c is None:
            print(f"{qn:<30}  MISSING"); ok = False; continue
        good = (c.verdict == want_verdict and c.is_flagged == want_flag)
        ok &= good
        print(f"{qn:<30} {c.score:>9.3f}  {c.verdict:<18} {str(c.is_flagged):<8} "
              f"{want_verdict}/{want_flag} {'OK' if good else '<-- MISMATCH'}")
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
