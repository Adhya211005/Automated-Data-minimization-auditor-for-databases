# `auditor/scoring/` — Necessity scoring engine (Phase 5)

The "brain". Combines the three independent signals into one decision per
column.

```
engine.py     ScoringEngine -> NecessityScore
__main__.py   end-to-end pipeline (ingest -> 3 engines -> score -> ranked report)
```

## The score

```
Necessity = sensitivity.score × (1 − usage.score) × retention.score
```

All three inputs are the 0–1 scores from Phases 2–4. The product is high only
when a column is sensitive **and** little-used **and** stale; any one factor
near 0 collapses it.

## `is_flagged` — the AND gate, not a score cutoff

A column is flagged only when **all three** cross an explicit threshold:

| condition | test | why this number |
|-----------|------|-----------------|
| `is_sensitive` | `sensitivity.score ≥ 0.50` | Phase 2's own `is_sensitive` cutoff; mid-way through its "medium" tier (bands: none <0.15, low <0.40, medium <0.70). On the seed it separates catalogue tier {high, medium} from {low, none} with no error. |
| `is_unused` | `usage.score ≤ 0.20` | Top of Phase 3's "rare" tier (unused <1e-9, rare <0.20, occasional <0.45). Captures "never queried" (0.0) and "read a handful of times months ago" (`ssn` = 0.11); excludes "occasionally queried" (`gender` = 0.25). |
| `is_stale` | `retention.score ≥ 0.50` | Phase 4 score 0.50 = oldest data overdue by **half a full retention period** — well past policy, not a rounding error (`score = clamp(days_overdue / policy_days, 0, 1)`). |

This is the difference from a plain PII scanner:
`score = 0.9 × (1 − 0.30) × 1.0 = 0.63` *looks* urgent, but `usage 0.30 > 0.20`
so it is **not flagged** — it's being used.

Thresholds are constructor args (`ScoringEngine(t_sensitive=…, t_unused=…,
t_stale=…)`); defaults live in `THRESHOLDS`.

## Verdict

```
not is_sensitive                        -> "not_a_risk"          (favorite_color)
is_sensitive, not is_unused             -> "keep"                (email: actively used)
is_sensitive, is_unused, not is_stale   -> "review"              (dormant, not overdue yet)
is_sensitive, is_unused, is_stale       -> "flag_for_deletion"
```

## Output — `NecessityScore`

`score`, `is_flagged`, `verdict`; the three sub-scores (`sensitivity`,
`usage`, `retention`) and their booleans (`is_sensitive`, `is_unused`,
`is_stale`); `breakdown` (each signal's score / threshold / pass / factor and
the product); `reasons` (plain-language); `evidence` (pii_type + matched
rules, read counts + last-access + services, days_overdue + policy) for the
dashboard drill-down.

## Run the whole pipeline

```powershell
python -m auditor.scoring                                    # live seed DB
python -m auditor.scoring --metadata m.json --log q.jsonl    # offline
python -m auditor.scoring --policy retention_policy.example.yaml
python -m auditor.scoring --top 15 | --flagged-only | --json
python -m auditor.scoring --evaluate                         # the four anchors
```

## Against the Phase 0 seed (default 365-day policy)

```
 #  column                        necessity  sens  1-use  stale  verdict
 1  users.mothers_maiden_name         0.900  0.90   1.00   1.00  flag_for_deletion
 2  users.ssn                         0.892  1.00   0.89   1.00  flag_for_deletion
 3  users.date_of_birth               0.495  0.85   0.58   1.00  keep      (queried for age checks)
 4  users.gender                      0.412  0.55   0.75   1.00  keep
 ...
 18 users.email                       0.017  0.95   0.02   1.00  keep      (10,727 reads)

flagged for deletion: 2 of 46   (flag_for_deletion=2, keep=12, not_a_risk=32)
```

**The four anchors** (`--evaluate`):

| column | verdict | flagged | matches |
|--------|---------|---------|---------|
| `users.email` | keep | no | sensitive **+ used** → keep regardless of staleness |
| `users.mothers_maiden_name` | flag_for_deletion | **yes** | sensitive + unused + stale |
| `users.favorite_color` | not_a_risk | no | unused + stale but **not sensitive** |
| `users.ssn` | flag_for_deletion | **yes** | spec §1.5 — "necessary-looking but never queried" |

With a longer policy (`--policy retention_policy.example.yaml`, users 1095 d)
`users` is no longer stale, so `mothers_maiden_name` / `ssn` drop to
**`review`** and nothing is flagged — the "sensitive + unused + fresh" case.

## Tests

`tests/test_scoring.py` — the four spec edge cases built from hand-made
signal objects (no DB); threshold-boundary tests; "big product but not
flagged"; the end-to-end pipeline against the seed confirming the anchors and
that only `mothers_maiden_name` + `ssn` are flagged.
