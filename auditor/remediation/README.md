# `auditor/remediation/` — Remediation script generator (spec module 6)

For a flagged column, drafts the SQL that actually removes the minimization
violation, with the column's evidence embedded as a comment so a reviewer
sees **why** before running anything.

**Draft only.** This module never opens a database connection and never
executes SQL. Every script is emitted as text.

## Strategy

| column is… | strategy | |
|------------|----------|--|
| a primary or foreign key | `anonymize_in_place` | can't safely drop a key; `UPDATE … SET NULL` / redact, with a warning that PK pseudonymization must be done across all referencing tables |
| anything else | `archive_then_drop` | copy the data to `dma_archive.<table>__<column>__<date>` (keyed by PK) for legal hold, then `ALTER TABLE … DROP COLUMN` |

Non-flagged verdicts (`keep`, `not_a_risk`) get `strategy: "none"` and a
one-line comment — no script.

## What a draft looks like (`users.mothers_maiden_name`)

```sql
-- ==========================================================================
-- Remediation draft  --  public.users.mothers_maiden_name
-- Generated 2026-09-09 by the Automated Data Minimization Auditor.
-- DRAFT ONLY - review and run manually. Nothing here has been executed.
--
-- WHY FLAGGED  (necessity score 0.900, verdict: flag_for_deletion)
--   Sensitivity  0.90  (>= 0.5)  knowledge_based, tier high
--                     matched: name:knowledge_based, name:identity_name_full, ...
--   Usage        0.00  (<= 0.2)  0 reads / 0 writes in the last 90d
--                     no application query touched this column
--   Staleness    1.00  (>= 0.5)
--                     oldest row 900d old - 535d past the 365-day policy
--                     (row age via created_at)
--
-- sensitive (knowledge_based) (score 0.90), not queried in the window ...
--
-- CAUTION: This script has not been run. Execute it manually after review.
-- CAUTION: Confirm no legal/contractual retention obligation for this data.
-- CAUTION: Take a fresh backup and run in a maintenance window.
-- ==========================================================================

BEGIN;

-- 1. Preserve the data for legal hold (keyed by "id").
CREATE SCHEMA IF NOT EXISTS "dma_archive";
CREATE TABLE "dma_archive"."users__mothers_maiden_name__20260909" AS
    SELECT "id", "mothers_maiden_name"
    FROM "public"."users"
    WHERE "mothers_maiden_name" IS NOT NULL;

-- 2. Drop the column.
ALTER TABLE "public"."users" DROP COLUMN "mothers_maiden_name";

COMMIT;
-- ROLLBACK;  -- run instead of COMMIT to abort

-- RESTORE (after COMMIT):
--   ALTER TABLE "public"."users" ADD COLUMN "mothers_maiden_name" TEXT;
--   UPDATE "public"."users" t SET "mothers_maiden_name" = a."mothers_maiden_name"
--     FROM "dma_archive"."users__mothers_maiden_name__20260909" a WHERE a."id" = t."id";

-- ALTERNATIVE - if only stale rows are the problem, delete those rows
-- (archive the full rows first), keeping the column for recent data:
--   CREATE TABLE "dma_archive"."users__overdue__20260909" AS
--     SELECT * FROM "public"."users" WHERE "created_at" < now() - interval '365 days';
--   DELETE FROM "public"."users" WHERE "created_at" < now() - interval '365 days';
```

Extra cautions fire when relevant:

- `pii_type` in {government_id, financial, health, credential} →
  "…often has a statutory retention period — confirm no legal obligation
  before dropping."
- `fraction_overdue < 1.0` (only some rows are old) → "…dropping removes the
  column for compliant rows too; delete only the overdue rows instead (see
  the ALTERNATIVE block)."
- `NOT NULL` column → noted.
- `--live` wasn't available so `fraction_overdue` is unknown → the caution is
  worded conservatively.

MySQL: backtick identifiers, and a "DDL is not transactional — back up first"
note instead of `BEGIN/COMMIT`.

## Use

```powershell
python -m auditor.remediation                 # drafts for every flagged column
python -m auditor.remediation --all            # ... and 'review' columns
python -m auditor.remediation --out drafts/     # one .sql file per column
```

```python
from auditor.remediation import RemediationGenerator
rem = RemediationGenerator().generate(necessity_score, col_meta, table_meta)
rem.sql        # the draft
rem.strategy   # "archive_then_drop"
rem.cautions   # list[str]
```

## API + dashboard

`record_run` generates a draft for every `flag_for_deletion` / `review`
column and stores it on the finding (`column_finding.remediation`). So:

- `GET /audits/{run_id}/columns/{name}` now includes `has_remediation` and
  (in the full response) `remediation`.
- `GET /audits/{run_id}/columns/{name}/remediation` returns just the draft;
  404 for `keep` / `not_a_risk` columns.

The dashboard's column detail panel shows it as a **"Suggested fix"** block —
strategy, cautions, the SQL in a scroll box, and a **Copy SQL** button.
Nothing in the UI can run it.

## Tests

`tests/test_remediation.py` — strategy selection, the archive/drop/restore
structure, every caution, keys → anonymize, non-flagged → none, MySQL
dialect, determinism, and that no executable `DELETE`/`DROP` ever appears
outside a comment. Plus the API tests in `tests/test_api.py`.
