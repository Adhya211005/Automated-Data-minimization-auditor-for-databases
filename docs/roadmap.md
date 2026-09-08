# Implementation Roadmap

One phase per work session. Each phase produces something runnable and
tested before the next begins.

This mirrors the 7 modules in [project-spec.md](project-spec.md) §5, with two
deliberate ordering changes:

1. **The FastAPI + persistence layer is its own phase (Phase 6), after the
   analysis modules — not the starting point.** The spec lists "Backend:
   FastAPI" first in the stack, but [CLAUDE.md](../CLAUDE.md) requires each
   engine (sensitivity, usage, retention) to be testable in isolation before
   scoring combines them. So they're built as plain Python packages with
   their own CLIs and unit tests first, then wrapped in an HTTP API once the
   full audit pipeline works end to end.
2. **The three analysis engines (Phases 2–4) are independent and can be
   built in any order or in parallel.** They're numbered here to match the
   spec's module numbers; nothing forces classifier-before-usage.

Everything that reads the target database uses a **read-only** connection
(CLAUDE.md). The write-capable `dma_seed` role from Phase 0 is only for the
seeder.

Proposed package layout (created incrementally):

```
auditor/
  config.py                 retention policy + settings loader
  ingestion/  connector.py   read-only SQLAlchemy engine
              metadata.py    schema + sample-value extraction
  sensitivity/ regex_rules.py / features.py / classifier.py
  usage/       parser.py     query log -> normalized column events
               scorer.py     per-column usage score
  retention/   policy.py / checker.py
  scoring/     necessity.py  Sensitivity x (1 - Usage) x Staleness
               rules.py      what counts as a violation
  remediation/ generator.py / llm.py (optional Groq)
  api/         main.py       FastAPI
  storage/     models.py     SQLite audit history
tests/
frontend/                    React dashboard (Phase 8)
```

---

## Phase 0 — Seed data generator ✅ (done)

**Covers:** a throwaway PostgreSQL database and synthetic query log so every
later phase has realistic input.

**Files/modules:** `seed-data/seed.py`, `seed-data/.env`,
`seed-data/output/` (`query_log.jsonl`, `query_log.csv`,
`column_catalog.csv`, `manifest.json`).

**Done looks like:**
- `python seed-data/seed.py --reset` builds `dma_auditor` with `users`,
  `orders`, `support_tickets` — sensitive, borderline, and harmless columns.
- ~18% of rows older than 400 days; some accounts idle 400+ days.
- 90-day query log where some columns are read constantly, some rarely, and
  some (`mothers_maiden_name`, `favorite_color`, …) never.
- `column_catalog.csv` records the intended sensitivity tier and expected
  usage for every column — the ground truth later phases score against.

---

## Phase 1 — Ingestion layer (DB Connector & Metadata Extractor)

**Covers:** connecting to a target database read-only and pulling everything
downstream modules need — table/column inventory, data types, nullability,
row counts, and a small sample of values per column. This is spec module 1,
"the eyes of the system."

**Files/modules:**
- `auditor/ingestion/connector.py` — builds a SQLAlchemy engine from a DSN,
  forces a read-only session (`default_transaction_read_only=on`, and
  reject if the role has write privileges), connection test.
- `auditor/ingestion/metadata.py` — SQLAlchemy `inspect()` to enumerate
  schemas/tables/columns; per-column sampled values (`TABLESAMPLE` or
  `ORDER BY random() LIMIT n`); per-table row count and min/max of timestamp
  columns.
- `auditor/config.py` — settings + target-DB profile loading.
- `tests/test_ingestion.py` — run against the Phase 0 `dma_auditor` DB.
- New role: `dma_auditor_ro` (SQL in `seed-data/README.md`).

**Done looks like:**
- `python -m auditor.ingestion --dsn ... --json` prints a metadata document:
  46 columns across 3 tables with type, nullable, row count, sample values,
  and detected timestamp columns.
- A write attempt through the connector raises (proves read-only).
- Works on the seeded PostgreSQL DB; MySQL support stubbed but not required
  yet.
- Tests pass with no network/DB writes.

---

## Phase 2 — Sensitivity / PII classifier

**Covers:** scoring each column 0–1 for how sensitive it is, from column
name + type + sample values. Regex/keyword baseline first, then a
lightweight scikit-learn model on top (spec module 2; CLAUDE.md: "start with
regex baseline first").

**Files/modules:**
- `auditor/sensitivity/regex_rules.py` — named patterns (email, phone,
  SSN-shaped, IP, card, DOB) matched against column names and sample values,
  each with a weight.
- `auditor/sensitivity/features.py` — turn a column's metadata + samples
  into a feature vector (name tokens, type, % of samples matching each
  pattern, entropy, avg length).
- `auditor/sensitivity/classifier.py` — `SensitivityClassifier` with
  `.predict(column_meta) -> {score, tier, pii_type, matched_rules}`;
  logistic-regression / gradient-boosted model trained on labels derived
  from `column_catalog.csv` plus a small hand-labeled set; falls back to the
  regex score when the model is unavailable.
- `tests/test_sensitivity.py` — assert `email`, `ssn`,
  `mothers_maiden_name` score high; `favorite_color`, `theme_preference`,
  `currency` score low; `last_login_ip` recognized despite the harmless
  name.

**Done looks like:**
- `python -m auditor.sensitivity --from-metadata meta.json` outputs a score
  + tier + pii_type per column.
- Regex baseline alone beats ~0.8 agreement with `column_catalog.csv` tiers;
  model improves on borderline columns (`last_login_ip`, `gender`,
  `support_tickets.body`).
- Module imports and runs with no database connection (operates on the
  Phase 1 metadata document).

---

## Phase 3 — Usage analyzer (Query Log Parser & Usage Scorer)

**Covers:** the core novel component. Parse the application query log,
attribute reads/writes to specific `table.column`s, and compute a per-column
usage score over a configurable window (spec module 3).

**Files/modules:**
- `auditor/usage/parser.py` — read `query_log.jsonl` / `.csv` (and, later,
  real `pg_stat_statements` / PostgreSQL CSV logs) into a normalized stream
  of `(ts, column, access_type, service)` events; a SQL-column extractor
  using `sqlglot` for logs that only carry raw SQL text.
- `auditor/usage/scorer.py` — `UsageScorer(window_days=90)` →
  per column: `read_count`, `write_count`, `last_read_at`, `distinct_services`,
  and a normalized `usage` in 0–1 (e.g. log-scaled read frequency, or
  recency-weighted). Columns absent from the log get `usage = 0`.
- `tests/test_usage.py` — feed a synthetic log; assert `users.email` → high,
  `users.ssn` → low-but-nonzero, `users.mothers_maiden_name` /
  `users.favorite_color` / `orders.notes` → exactly 0.

**Done looks like:**
- `python -m auditor.usage --log seed-data/output/query_log.jsonl --window 90`
  prints the per-column usage table (matches the summary the Phase 0 seeder
  already prints).
- Correctly reports the "never queried" set with zero false negatives
  against `column_catalog.csv`'s `expected_usage = never` rows.
- Runs standalone on the log file — no DB dependency.

---

## Phase 4 — Retention policy engine

**Covers:** deciding whether data in a column/table is overdue against a
configurable retention policy, using its timestamp columns and row ages
(spec module 4).

**Files/modules:**
- `auditor/config.py` — `retention_policy.yaml`: default max-age plus
  per-table / per-purpose overrides (e.g. `support_tickets: 365d`,
  `orders: 2555d` for tax).
- `auditor/retention/policy.py` — load + validate policy, resolve the
  applicable rule for a table.
- `auditor/retention/checker.py` — `RetentionChecker` → per table:
  `% rows past policy`, `oldest_row_age`, and per timestamp-bearing column a
  `staleness` in 0–1 (fraction of rows / how far past policy). Read-only
  aggregate queries via the Phase 1 connector.
- `tests/test_retention.py` — against seeded data: `support_tickets` and the
  400-day user slice flagged stale; fresh rows not flagged; a table with no
  timestamp column handled gracefully.

**Done looks like:**
- `python -m auditor.retention --dsn ... --policy retention_policy.yaml`
  reports staleness per table/column.
- Numbers line up with the seeder's stale counts (~325 users, 166 orders,
  81 tickets > 400 days).
- Policy file is the only thing you change to re-tune it; no code edits.

---

## Phase 5 — Necessity scoring & correlation engine

**Covers:** combine the three signals into one **Necessity Score** per
column and apply violation rules (spec module 5, "the brain").

`Necessity Score = Sensitivity × (1 − Usage) × Staleness`

**Files/modules:**
- `auditor/scoring/necessity.py` — takes the three module outputs keyed by
  `table.column`, aligns them, computes the score, returns a ranked list
  with the contributing evidence attached.
- `auditor/scoring/rules.py` — violation predicate (e.g. flag when
  `sensitivity ≥ 0.6 AND usage ≤ 0.05 AND staleness ≥ 0.5`), configurable
  thresholds, verdict labels (`keep` / `review` / `flag_for_deletion`).
- `tests/test_scoring.py` — the three edge cases CLAUDE.md names explicitly:
  1. sensitive + used (`email`) → not flagged
  2. sensitive + unused + fresh → not flagged
  3. sensitive + unused + stale (`mothers_maiden_name`) → flagged
  plus: harmless + unused + stale (`favorite_color`) → not flagged.

**Done looks like:**
- `python -m auditor.audit --dsn ... --log ... --policy ...` runs all four
  modules and prints a ranked table: column, score, the three sub-scores,
  verdict.
- On the seeded dataset the top of the list is `mothers_maiden_name`, with
  `ssn` and `orders.billing_address` also flagged; `email`, `password_hash`,
  `favorite_color` are not.
- All four scoring edge-case tests pass.

---

## Phase 6 — API + audit history persistence

**Covers:** wrap the Phase 5 pipeline in a FastAPI service and persist each
audit run so the dashboard can show history and before/after trends (spec
stack: FastAPI backend, SQLite/Postgres audit history).

**Files/modules:**
- `auditor/storage/models.py` — SQLite (SQLАlchemy) schema: `audit_run`,
  `column_finding`, `run_target`.
- `auditor/api/main.py` + `routes.py`:
  - `POST /audits` — start a run against a registered target, persist findings
  - `GET /audits/{id}` — findings + evidence for one run
  - `GET /columns/{table.column}/history` — score over time
  - `GET /targets` — registered databases
- `tests/test_api.py` — FastAPI `TestClient`: run an audit against
  `dma_auditor`, assert findings persisted and retrievable.

**Done looks like:**
- `uvicorn auditor.api.main:app` serves the endpoints; `POST /audits`
  against the seeded DB returns a run id and stores findings.
- Re-running produces a second row per column, so history queries return a
  trend.
- OpenAPI docs at `/docs` list every endpoint.

---

## Phase 7 — Remediation script generator

**Covers:** for the top-flagged columns, auto-draft an actionable
remediation — an archive/`ALTER TABLE ... DROP COLUMN` migration or an
anonymization `UPDATE`, plus a plain-language justification (spec module 6,
"the hands"). Optional Groq call for the human-readable explanation.

**Files/modules:**
- `auditor/remediation/generator.py` — per finding, emit: a reversible
  archive step (copy to `*_archive` table), the destructive DDL, and a
  rollback note. Never executed — output only.
- `auditor/remediation/llm.py` — optional Groq call turning the evidence
  bundle into a paragraph ("PII, unused for 9 months, 400+ days past a
  365-day policy — archive and drop"); templated fallback when no API key.
- API: `GET /audits/{id}/remediation` returns the script + narrative.
- `tests/test_remediation.py` — generated SQL parses; archive step precedes
  the drop; dry-run only, no execution path.

**Done looks like:**
- For `mothers_maiden_name` the tool outputs a ready-to-review `.sql` file
  (archive → drop) and a one-paragraph rationale.
- Works with and without a Groq API key.
- Nothing in this module can write to the target DB.

---

## Phase 8 — React dashboard

**Covers:** the human interface for DPOs/DBAs — ranked findings, drill-down
evidence, and a before/after risk-reduction view (spec module 7).

**Files/modules:**
- `frontend/` — React app (Vite). Views:
  - findings table (sortable by Necessity Score, filter by verdict/table)
  - column detail: the three sub-scores, sample query-log evidence,
    row-age histogram, generated remediation script
  - run history / trend chart per column
  - summary: total sensitive columns vs. flagged, projected risk reduction
- Talks to the Phase 6 API only.

**Done looks like:**
- `npm run dev` in `frontend/` shows the flagged columns from a real audit
  run of `dma_auditor`, `mothers_maiden_name` at the top.
- Clicking a column shows its evidence and the draft SQL.
- The summary view shows "N sensitive columns, M flagged, fix these first."

---

## Cross-cutting (every phase)

- Read-only DB access, always.
- Each analysis module runs standalone via `python -m auditor.<module>` and
  has its own tests before it's wired into scoring.
- `column_catalog.csv` from Phase 0 is the evaluation ground truth for
  Phases 2–5.
