# `auditor/retention/` — Retention policy engine (Phase 4)

Answers **"is this data overdue?"** — the `Staleness` term of
`Necessity = Sensitivity × (1 − Usage) × Staleness`.

Deliberately the simplest of the three signals: a day-threshold policy vs the
oldest timestamp in each table.

```
policy.py    RetentionPolicy — default_days + per-table / per-column overrides
checker.py   RetentionChecker -> ColumnRetention / TableRetention
__main__.py  CLI
```

## Use

```powershell
python -m auditor.retention                                  # default 365d, metadata only
python -m auditor.retention --policy retention_policy.example.yaml
python -m auditor.retention --default-days 730
python -m auditor.retention --live                           # + count rows past policy
python -m auditor.retention --json
python -m auditor.retention --evaluate                       # seed sanity check
```

```python
from auditor.retention import RetentionChecker, RetentionPolicy
res = RetentionChecker(RetentionPolicy(default_days=365), metadata).check()
res.column("users.mothers_maiden_name").score        # 1.0 — inherits the stale user row
res.column("users.date_of_birth").basis              # "table:created_at", not "self"
```

## Policy format (YAML or JSON)

```yaml
default_days: 365
tables:
  orders: 2555              # 7 years for tax
  support_tickets: 365
columns:
  users.last_login_at: 730
anchors:
  users: created_at         # which timestamp marks a row's age (default: created_at)
exclude:
  - "*.updated_at"          # glob on table.column — never flag these
```

Resolution: **column override → table override → `default_days`**.

## Output — `ColumnRetention` (parallel to `ColumnSensitivity` / `ColumnUsage`)

| field | meaning |
|-------|---------|
| `score` | 0–1 staleness — the `Staleness` term |
| `is_overdue` | oldest row older than policy |
| `days_overdue` | `oldest_row_age − policy_days` (None if not overdue) |
| `policy_applied` | `{"days": 365, "source": "table", "anchor": "created_at"}` |
| `oldest_row_age_days` / `newest_row_age_days` | span of the anchor timestamp |
| `basis` | `self` (temporal col scored on its own values) · `table:<anchor>` (inherited) · `excluded` · `no-temporal-data` |
| `rows_total` / `rows_overdue` / `fraction_overdue` | only with `--live` |

`TableRetention` carries the same fields at table granularity.

### Score

```
oldest_age   = now − min(anchor timestamp)                (Phase 1 metadata)
days_overdue = oldest_age − policy_days
age_score    = clamp(days_overdue / policy_days, 0, 1)    # data 2× past policy → 1.0
score        = age_score                                   (metadata only)
             = age_score × (0.4 + 0.6 × volume)            (--live: volume =
               min(1, fraction_overdue / 0.10))
```

### Why columns inherit a table score

Staleness is a property of the **row**, not the column. `mothers_maiden_name`
has no timestamp of its own — but the user row it sits in can be 900 days
old. So every non-lifecycle column takes its table's score, computed from an
anchor column (`created_at` by default). Columns that *are* lifecycle
timestamps (`updated_at`, `resolved_at`, `last_login_at`) are scored on their
own values instead (`basis = "self"`).

**`date_of_birth` is temporal but not a lifecycle timestamp** — it's an
attribute date. The name heuristic (`is_retention_relevant`) excludes
`birth` / `dob` / `expiry` / `valid_from` / `*_start_date` etc., so DOB
inherits the row age instead of being (wrongly) scored on a 1950s value.

## Against the Phase 0 seed

~18% of `users` / `orders` / `support_tickets` rows were seeded 400+ days old.

| policy | result |
|--------|--------|
| default 365 d | all three tables **overdue, score 1.000** (oldest rows 836–900 days → >2× policy) |
| `retention_policy.example.yaml` (orders 2555 d, users 1095 d) | only `support_tickets` overdue; `orders` / `users` clear (score 0.000) |
| 3650 d | nothing overdue |

`--live` adds row counts: users 59% / support_tickets 32% / orders 18% of
rows past a 365-day policy — matching the seeding.

`users.date_of_birth` → `basis = table:created_at` (inherits, not scored on
its 1940 minimum). `users.mothers_maiden_name` → score 1.0, 535 days overdue
— the spec's worked example.

## Limitations (kept deliberately small)

- Threshold policy only — no "keep N most recent", no legal-basis modelling,
  no per-row exceptions.
- One anchor timestamp per table. A table with no lifecycle timestamp gets
  `basis = "no-temporal-data"` and score 0 (can't assess it).
- `age_score` saturates at 1.0 once data is 2× past policy — no gradient
  beyond that from age alone (`--live` volume still varies it).
- The metadata-only path trusts `min(anchor)` from Phase 1; it doesn't know
  if that one oldest row is an outlier. `--live` `fraction_overdue` is the
  corrective.

## Tests

`tests/test_retention.py` — `is_retention_relevant` heuristic, policy
resolution, the score curve, synthetic old/fresh tables (no DB), and the live
seed (all tables overdue at 365 d, clear at 3650 d, DOB inherits, example
policy discriminates).
