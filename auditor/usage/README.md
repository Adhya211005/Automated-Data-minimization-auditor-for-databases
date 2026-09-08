# `auditor/usage/` — Usage analyzer (Phase 3)

**The project's core novelty.** Treats "necessity" as an observable property:
does live application traffic actually read this column?

```
logsource.py   read query_log.jsonl / .csv -> LogEntry records
parser.py      QueryParser: SQL statement -> {reads, writes} columns (sqlglot)
analyzer.py    UsageAnalyzer -> ColumnUsage (score, counts, last-access, evidence)
evaluation.py  parser fidelity + score-vs-ground-truth
__main__.py    CLI
```

## Use

```powershell
python -m auditor.usage                    # analyze the seed log, per-column table
python -m auditor.usage --json             # ColumnUsage records
python -m auditor.usage --evaluate         # both evaluation checks
python -m auditor.usage --evaluate-parser  # just SQL-parser fidelity
python -m auditor.usage --log FILE --metadata FILE --window 90 --mode hybrid
```

```python
from auditor.usage import analyze_log
res = analyze_log("seed-data/output/query_log.jsonl", metadata=meta, window_days=90)
res.column("users.mothers_maiden_name").score   # 0.0
res.column("users.email").score                 # ~0.98
```

## Output — `ColumnUsage` (parallel to Phase 2's `ColumnSensitivity`)

| field | meaning |
|-------|---------|
| `score` | 0–1 usage — feeds **`(1 - score)`** in `Necessity = Sensitivity × (1 − Usage) × Staleness` |
| `is_used` | did **any** query in the window touch it (binary, unambiguous) |
| `tier` | `unused` / `rare` / `occasional` / `frequent` / `heavy` |
| `read_count` / `write_count` / `total_count` | raw counts in the window |
| `last_read_at` / `last_write_at` / `last_access_at` / `first_access_at` | ISO timestamps — dashboard evidence |
| `distinct_days` / `distinct_services` | spread of access |
| `access_by_type` | `{"read": n, "write": m}` |
| `access_by_service` | `{"web-app": n, "support-console": m, …}` |
| `query_templates` | which query shapes touched it |
| `attribution` | `sqlglot` / `declared` / `hybrid` — how the columns were resolved |
| `signals` | the sub-scores (rank fraction, absolute component, recency) |

### Score formula

```
weighted  = reads + 0.25 * writes            # reads dominate
rank      = percentile rank of `weighted` among ALL columns (0-usage included)
absolute  = log1p(weighted) / log1p(400)     # a quiet DB shouldn't look busy
freq      = 0.55 * rank + 0.45 * absolute
recency   = 0.5 ** (days_since_last_access / (window/3))
score     = 0                                        if never accessed
          = freq * (0.5 + 0.5 * recency)             otherwise
```

`is_used` is the plain "count > 0" — that's the signal the never-queried
columns trip, and it's exact.

## Evaluation (`--evaluate`)

Against the Phase 0 seed:

```
binary  used vs unused         precision 1.000   recall 1.000   (TP=41 FP=0 TN=5 FN=0)
Spearman(score, log-derived label)   0.957     <- objective: buckets of real log frequency
Spearman(score, catalogue label)     0.712     <- design-intent label (see caveat)

anchors:  mothers_maiden_name 0.000 | favorite_color 0.000 | orders.notes 0.000
          users.ssn 0.108 (rare, months since last read) | users.email 0.982
```

The 5 never-queried columns score exactly 0; the frequently-queried ones score
0.8–1.0; `ssn` (read 4× in 90 days, last read ~80 days ago) sits at 0.11.

**Caveat on the 0.712 number:** three catalogue labels (`users.phone`,
`users.account_status`, `users.marketing_consent` = "low") and two more
(`orders.billing_address` = "rare", `orders.promo_code` = "low") were written
in Phase 0 from *design intent*. The query-log templates actually read/write
those columns thousands of times (`load_user_profile` reads phone on every
call). The analyzer reports the real traffic, so it "disagrees" with those
labels — correctly. `--evaluate` lists every such disagreement. The
log-derived Spearman (0.957) buckets columns straight from the log's own
counts and has no such inconsistency.

## Limitations — what the parser does NOT handle yet

Be honest about coverage. On the seed's 16 distinct statements: **15/16 parse
with sqlglot**, 1 uses the INSERT regex fallback, 0 fail. Column attribution
vs the log's declared lists: **precision 0.965, recall 0.891**.

- **`SELECT *` needs a schema.** With metadata it's expanded to real columns;
  without, it's recorded as a `table.*` wildcard and (in the analyzer) either
  expanded from metadata or skipped.
- **INSERT … VALUES with elided/parameterised values** — the Phase 0 seeder
  writes `INSERT INTO orders (…) VALUES (...)`, which is not valid SQL.
  sqlglot rejects it; a regex recovers the column list. A real
  `pg_stat_statements` entry (`VALUES ($1, $2, …)`) parses fine.
- **Columns a query touches but doesn't name.** `INSERT … SELECT` reads are
  followed, but an `INSERT` whose values come from application variables
  (`users.id` looked up in code, then inserted) can't be seen in the SQL —
  only the log's declared list has it. Hence **`mode="hybrid"` is the
  default**: union of the SQL parse and the log's `columns_read` /
  `columns_written`. `mode="sql"` is parser-only; `mode="declared"` trusts
  the log fields.
- **CTE / derived-table columns** are traced to base columns when their
  SELECT is visible; a column projected by a CTE under an alias that can't be
  resolved to a base table is reported in `unresolved`, not counted.
- **Unqualified columns in multi-table queries** are resolved only when the
  schema makes the owning table unambiguous; otherwise `unresolved`.
- **No cost/row weighting.** A query that scans 1M rows and one that reads 1
  count the same. Frequency, not volume.
- **`EXPLAIN`, `EXECUTE`, dynamic SQL, stored-procedure bodies** are not
  analyzed — they fall back to declared columns or are skipped.
- **Aggregates / expressions**: `count(*)` is correctly *not* treated as a
  wildcard; `date_part('year', age(dob))` correctly attributes `dob`.

## Tests

- `tests/test_usage_parser.py` — 17 parser unit tests, no DB (SELECT/JOIN/
  GROUP BY/subquery/CTE/UPDATE/INSERT/DELETE, alias resolution, `SELECT *`,
  the `count(*)` regression).
- `tests/test_usage.py` — scoring in isolation on synthetic logs + evaluation
  against the live seed (binary P/R = 1.0, `spearman_log_derived` ≥ 0.9,
  parser precision ≥ 0.9).
