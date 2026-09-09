# Automated Data Minimization Auditor for Databases

**BCSE410L — Cyber Security coursework project.**

A tool that scans a live PostgreSQL database and flags columns that are
**minimization violations** — but only when *all three* of these hold at once:

| signal | question | phase |
|--------|----------|-------|
| **Sensitivity** | is this column personal / risky data? | 2 |
| **Usage** | has any application query actually read it lately? | 3 |
| **Staleness** | is the data past its retention policy? | 4 |

```
Necessity Score = Sensitivity × (1 − Usage) × Staleness
```

A column is **flagged for deletion** only when it crosses the threshold on
**sensitive AND unused AND stale** — not when the product happens to be large.
That AND-gate is the point: a plain PII scanner flags `email` and
`mothers_maiden_name` identically; this tool flags `mothers_maiden_name`
(sensitive, never queried, 500+ days overdue) and leaves `email` alone
(sensitive but read 10,000× a week). The output is a *prioritised,
evidence-backed* list with a ready-to-review SQL migration for each finding —
not "here are 200 PII columns, go figure it out".

## Pipeline

```
 target PostgreSQL DB (read-only)                 company retention policy doc
        │                                                   │  (optional, stretch)
        ▼                                                   ▼
 ┌─────────────┐   ┌──────────────┐                 ┌────────────────────┐
 │  ingestion  │──▶│  sensitivity │─┐               │  policy_extractor  │
 │ (Phase 1)   │   │  (Phase 2)   │ │               │  NLP → RetentionPolicy
 │ schema +    │   ├──────────────┤ │               └─────────┬──────────┘
 │ samples,    │──▶│  usage       │─┼──▶ scoring ───────┐      │
 │ read-only   │   │  (Phase 3)   │ │   (Phase 5)       │      ▼
 │ connector   │   ├──────────────┤ │   Necessity Score │  ┌────────────┐
 │             │──▶│  retention   │─┘   + AND-gate      │  │ retention  │
 │             │   │  (Phase 4)   │◀───────────────────────│  checker   │
 └─────────────┘   └──────────────┘                    │  └────────────┘
        │                                              ▼
        │                                     ┌──────────────────┐
        │                                     │  remediation gen │  draft SQL fix
        │                                     │  (evidence as    │  per flagged column
        │                                     │   SQL comments)  │
        ▼                                     └──────────────────┘
 ┌──────────────────────────────┐                      │
 │  FastAPI + SQLite history    │◀─────────────────────┘
 │  (Phase 6)  /audits, compare │
 └──────────────┬───────────────┘
                ▼
 ┌──────────────────────────────┐
 │  React dashboard (Phase 7)   │  report → column evidence → suggested fix → compare runs
 └──────────────────────────────┘
```

Each analysis engine is independent and independently tested. The regex
sensitivity classifier is the pipeline default; an ML model and a
policy-document NLP extractor are **stretch features** (see
[Stretch features](#stretch-features)).

---

## Prerequisites

| | version used | notes |
|---|---|---|
| PostgreSQL | 18 | any 12+; installed as a local service, listening on `localhost:5432` |
| Python | 3.14 | any 3.11+ |
| Node.js | 24 | any 18+ (for the dashboard only) |
| `psql` on PATH | — | ships with PostgreSQL, e.g. `C:\Program Files\PostgreSQL\18\bin` |

You need the **`postgres` superuser password** (set when PostgreSQL was
installed) for two one-time `CREATE ROLE` commands in step 2.

---

## Setup from a clean clone

All commands are **PowerShell**, run from the repo root. Do them in order.

### 1. Python environment + dependencies

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt          # the auditor
pip install -r seed-data\requirements.txt # the seed-data generator
```

### 2. PostgreSQL roles + database (one time, as the `postgres` superuser)

```powershell
$env:Path += ";C:\Program Files\PostgreSQL\18\bin"

# a write-capable role that owns the throwaway database and seeds it
psql -U postgres -c "CREATE ROLE dma_seed WITH LOGIN PASSWORD 'dma_seed_local_pw' CREATEDB;"
psql -U postgres -c "CREATE DATABASE dma_auditor OWNER dma_seed;"

# the read-only role the auditor uses — LOGIN + SELECT only, scoped to this DB
psql -U postgres -f migrations\001_create_readonly_role.sql
```

`migrations\001_create_readonly_role.sql` is idempotent and prints the
resulting privileges (expect `SELECT` only). It also runs
`ALTER DEFAULT PRIVILEGES` so tables created later by `dma_seed` are
auto-granted `SELECT` to `dma_auditor_ro` — you never re-run it after a reseed.

### 3. Config files (gitignored — create from the examples)

```powershell
Copy-Item seed-data\.env.example seed-data\.env
Copy-Item auditor\.env.example   auditor\.env
```

The example values match the roles created in step 2. Change the passwords in
**both** places if you used different ones.

### 4. Seed the database + generate the synthetic query log

```powershell
python seed-data\seed.py --reset
```

Creates `users` / `orders` / `support_tickets` (46 columns — a deliberate mix
of sensitive, borderline, and harmless), fills them with `Faker` data where
~18% of rows are 400+ days old, and writes a synthetic 90-day query log
(`seed-data\output\query_log.jsonl`) where some columns are queried constantly,
some rarely, and `mothers_maiden_name` / `favorite_color` never.
Re-run with `--reset` any time to get back to a known state. (~11 s)

### 5. Backend API

```powershell
uvicorn auditor.api.main:app --port 8000
```

`http://127.0.0.1:8000/docs` for the OpenAPI UI. First launch auto-creates the
SQLite history DB at `auditor\audit_history.db`.

### 6. Dashboard (separate terminal)

```powershell
cd frontend
npm install
npm run dev
```

Opens `http://localhost:5173`. `npm run dev` proxies `/api/*` → `:8000`, so no
CORS setup is needed. Trigger the first audit with the **“+ New audit”**
button (or, from a terminal, `curl.exe -X POST http://127.0.0.1:8000/audits -H "content-type: application/json" -d "{}"` —
note `curl.exe`, since PowerShell's `curl` is an alias for `Invoke-WebRequest`).

---

## Timing

Measured on the reference machine (PostgreSQL 18 local, Python 3.14), fresh
audit trigger → ranked result visible:

| | time |
|---|---|
| pipeline only (`run_audit`) | **~1.0 s** (0.8–1.3 s) |
| `POST /audits` round-trip (pipeline + persist + read-back) | **~1.0 s** (first after server start ~1.4 s) |
| trigger from the dashboard → table re-rendered | **~2 s** |

Breakdown of the pipeline second: metadata extraction from Postgres ~0.5 s,
usage analysis over the 30 k-entry query log ~0.4 s, everything else < 0.1 s.

> **Demo caveat — OneDrive.** The reference checkout lives under a
> OneDrive-synced folder. Right after `seed.py --reset` regenerates the 15 MB
> query log, OneDrive re-syncs it and audits spike to 5–15 s until sync
> settles (~1 min). **Pause OneDrive (or wait a minute) before demoing.**
> Steady state is ~1 s. A non-synced checkout does not have this issue.

---

## Testing

```powershell
python -m pytest -q
```

**213 passed.** (~20 s warm; ~130 s from a truly cold start — the first run
trains the ML model and OneDrive is still syncing the fresh seed.)

Tests that need PostgreSQL skip cleanly with a message if the DB or the
`dma_auditor_ro` role is missing. Coverage per module:

| module | tests | headline |
|--------|------:|----------|
| `tests/test_connector.py`, `test_phase1_acceptance.py` | 24 | read-only enforcement — write attempts raise `42501` even in a forced read-write transaction |
| `tests/test_metadata.py` | 10 | the `DatabaseMetadata` contract (PK/FK, temporal spans, samples, JSON round-trip) |
| `tests/test_sensitivity.py` | 38 | regex classifier — precision/recall **1.0** vs the seed catalogue |
| `tests/test_usage_parser.py`, `test_usage.py` | 30 | SQL→columns parser + usage score (Spearman **0.957** vs log-derived truth) |
| `tests/test_retention.py` | 27 | staleness, row-anchor inheritance, `date_of_birth` treated as an attribute not a lifecycle stamp |
| `tests/test_scoring.py` | 13 | the four spec edge cases + AND-gate (a 0.63 product that is *not* flagged) |
| `tests/test_api.py` | 10 | the three endpoints + reruns are byte-identical + `/compare` catches a threshold crossing |
| `tests/test_remediation.py` | 12 | draft SQL structure, escalating cautions, no executable `DELETE`/`DROP` outside comments |
| `tests/test_sensitivity_ml.py` | 17 | features, synthetic corpus, ML vs regex side-by-side |
| `tests/test_policy_extractor.py` | 20 | NLP extraction + **byte-identical** to a hand-written policy through the checker |

---

## Where things are

```
auditor/
  ingestion/     Phase 1 — ReadOnlyConnector, MetadataExtractor → DatabaseMetadata
  sensitivity/   Phase 2 — RegexSensitivityClassifier (+ ML/hybrid, stretch)
  usage/         Phase 3 — QueryParser (sqlglot) + UsageAnalyzer
  retention/     Phase 4 — RetentionPolicy + RetentionChecker (+ policy_extractor, stretch)
  scoring/       Phase 5 — ScoringEngine → NecessityScore, and pipeline.py (the whole run)
  remediation/   Phase 7 stretch — draft SQL fix per flagged column
  storage/       Phase 6 — SQLite audit history (SQLAlchemy)
  api/           Phase 6 — FastAPI app
frontend/        Phase 7 — React + Vite dashboard
migrations/      the read-only role SQL
seed-data/       Phase 0 — seed.py, the synthetic query log, sample_retention_policy.md
docs/            project-spec.md, roadmap.md, DEMO.md
tests/
```

Every `auditor/<module>/` has its own `README.md` with the design detail and
its evaluation numbers. Each module also has a CLI:

```powershell
python -m auditor.ingestion --check          # prove the connection is read-only
python -m auditor.sensitivity --evaluate     # regex classifier vs the catalogue
python -m auditor.usage --evaluate           # usage score vs log-derived truth
python -m auditor.retention --policy retention_policy.example.yaml --live
python -m auditor.scoring                    # the full ranked report
python -m auditor.scoring --flagged-only     # just the violations + why
python -m auditor.remediation                # draft SQL for every flagged column
```

---

## Design decisions

- **The auditor is read-only, enforced three ways.** A `SELECT`-only role
  (`dma_auditor_ro`), `default_transaction_read_only = on` on every session,
  and a statement guard that raises on anything that isn't a read. The
  connector *verifies* all three at construction and refuses to proceed
  otherwise. `tests/test_phase1_acceptance.py` confirms the database itself
  rejects writes even when the session is forced read-write.
- **Regex sensitivity is the default, not the ML model.** A compliance tool
  has to justify every flag with a concrete rule; the regex classifier does
  that and scores 1.0 precision/recall on the independent seed labels. The ML
  model (trained on the regex classifier's own output) is available but
  *approximates* the rules rather than independently discovering sensitivity —
  stated plainly in `auditor/sensitivity/README.md`.
- **Flagging is an AND-gate on explicit thresholds**
  (`sensitivity ≥ 0.50`, `usage ≤ 0.20`, `staleness ≥ 0.50`), each derived
  from and documented against the producing phase's own score bands — not a
  cutoff on the multiplied score.
- **The remediation generator never touches a database.** It emits draft SQL
  as text, with the necessity evidence embedded as comments and the
  destructive `DELETE`/`DROP` inside an ALTERNATIVE comment block.
- **Audit runs are persisted and comparable.** Re-running against unchanged
  data is byte-identical; `/audits/compare` surfaces newly-flagged /
  no-longer-flagged / verdict-changed between two runs — the recurring-audit
  workflow a DPO actually has.

---

## Stretch features

Both are opt-in and do **not** change the core pipeline.

- **ML sensitivity classifier** — `auditor/sensitivity/ml_classifier.py`.
  scikit-learn logistic regression + a hybrid regex/ML blend.
  `python -m auditor.sensitivity --compare`. Matches the regex baseline's
  precision/recall on the seed; the hybrid nudges 4-tier accuracy 0.804 →
  0.826. Honest limitation (labels come from the rules) is documented.
- **Retention policy NLP extractor** — `auditor/retention/policy_extractor.py`.
  Reads a written retention policy
  (`seed-data/sample_retention_policy.md`), pulls out `<category> → N days`
  rules by regex + cue words, maps them to schema objects, and emits a
  `RetentionPolicy` in the exact Phase 4 shape.
  `python -m auditor.retention.policy_extractor --check`. Closes the
  literature-survey gap (existing tools do document-NLP *or* live-DB
  analysis, never both). Framed as an assistive draft a human reviews —
  the category→column mapping is inherently ambiguous.

---

## Project status

All build phases (0–7) plus three stretch features (remediation generator,
ML classifier, policy extractor) are complete. See `CLAUDE.md` for the
phase-by-phase log and `docs/roadmap.md` for the original plan.
