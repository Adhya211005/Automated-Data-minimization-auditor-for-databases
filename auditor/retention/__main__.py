"""CLI for the retention policy engine.

    python -m auditor.retention                       default policy (365d), metadata only
    python -m auditor.retention --policy p.yaml        load a policy file
    python -m auditor.retention --default-days 730     override just the default
    python -m auditor.retention --live                 also query row counts past policy
    python -m auditor.retention --json
    python -m auditor.retention --evaluate             check the seed's old rows trip staleness
"""

from __future__ import annotations

import argparse
import json
import sys

from auditor.retention.checker import RetentionChecker
from auditor.retention.policy import RetentionPolicy


def _load_metadata(path: str | None):
    if path:
        from auditor.ingestion.metadata import DatabaseMetadata
        return DatabaseMetadata.load(path)
    from auditor.ingestion import extract_metadata
    return extract_metadata()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auditor.retention", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy", metavar="FILE", help="retention policy (.yaml / .json)")
    ap.add_argument("--default-days", type=int, help="override default_days")
    ap.add_argument("--metadata", metavar="FILE", help="metadata JSON; omit to extract live")
    ap.add_argument("--live", action="store_true",
                    help="also run one aggregate query per table for rows-past-policy counts")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args(argv)

    policy = RetentionPolicy.from_file(args.policy) if args.policy else RetentionPolicy()
    if args.default_days:
        policy.default_days = args.default_days

    connector = None
    if args.live or args.metadata is None:
        from auditor.ingestion.connector import ReadOnlyConnector, ReadOnlyViolation
        try:
            connector = ReadOnlyConnector.from_env(strict=False)
        except ReadOnlyViolation as exc:
            print(f"note: no live connection ({exc}); using metadata only", file=sys.stderr)
            connector = None

    meta = _load_metadata(args.metadata)
    checker = RetentionChecker(policy, meta, connector=connector if args.live else None)
    result = checker.check()

    if args.evaluate:
        return _evaluate(result)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print(f"policy: default={policy.default_days}d  "
          f"tables={policy.tables or '{}'}  now={result.now[:10]}  "
          f"live={'yes' if result.used_connector else 'no'}\n")
    print(f"{'table':<20} {'anchor':<14} {'policy':>7} {'oldest':>8} {'over':>7}  frac  score")
    print("-" * 74)
    for t in sorted(result.tables, key=lambda x: -x.score):
        frac = f"{t.fraction_overdue:.0%}" if t.fraction_overdue is not None else "  -"
        print(f"{t.name:<20} {str(t.anchor or '-'):<14} {t.policy_days:>6}d "
              f"{str(t.oldest_row_age_days or '-'):>7}d {str(t.days_overdue or '-'):>6}  "
              f"{frac:>5}  {t.score:.3f}")
    print("\n" + result.summary_table())
    return 0


def _evaluate(result) -> int:
    """The Phase 0 seed put ~18% of rows in users/orders/support_tickets past
    400 days. With a policy <= 365d all three should be overdue; a big policy
    should clear them; date_of_birth must never score."""
    ok = True
    for name in ("users", "orders", "support_tickets"):
        t = result.table(name)
        status = "OVERDUE" if t.is_overdue else "ok"
        flag = "" if t.is_overdue and t.score > 0 else "  <-- expected overdue!"
        if flag:
            ok = False
        print(f"{name:<18} score={t.score:.3f} oldest={t.oldest_row_age_days}d "
              f"policy={t.policy_days}d {status}{flag}")

    dob = result.column("users.date_of_birth")
    if dob:
        note = "" if dob.basis.startswith("table:") else "  <-- should inherit table, not self"
        if dob.basis == "self":
            ok = False
        print(f"\nusers.date_of_birth: score={dob.score:.3f} basis={dob.basis}{note}")

    mmn = result.column("users.mothers_maiden_name")
    if mmn:
        print(f"users.mothers_maiden_name: score={mmn.score:.3f} overdue={mmn.is_overdue} "
              f"days_overdue={mmn.days_overdue} (inherits {mmn.basis})")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
