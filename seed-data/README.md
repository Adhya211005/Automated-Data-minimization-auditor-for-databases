# Phase 0 — Seed data generator

Builds a realistic, throwaway PostgreSQL database plus a synthetic 90-day
application query log, so the later phases (sensitivity classifier, usage
analyzer, retention checker, scoring engine) have something real to chew on.

Everything is fake — `Faker`-generated names, emails, SSN-shaped strings, etc.
No real personal data is involved.

## One-time setup

### 1. Python deps

```powershell
pip install -r seed-data/requirements.txt
```

### 2. Dedicated PostgreSQL role + database

Run these once as a superuser (the installer's `postgres` account):

```powershell
$env:Path += ";C:\Program Files\PostgreSQL\18\bin"
psql -U postgres -c "CREATE ROLE dma_seed WITH LOGIN PASSWORD 'dma_seed_local_pw' CREATEDB;"
psql -U postgres -c "CREATE DATABASE dma_auditor OWNER dma_seed;"
```

`dma_seed` is intentionally minimal: it can log in and create/own its own
database, nothing more. The seeder is the only thing that writes to
`dma_auditor`.

### 3. Config

```powershell
copy seed-data\.env.example seed-data\.env
```

`.env` already matches the role above. Change the password in **both** places
if you want something else. `.env` is gitignored.

## Run

```powershell
python seed-data/seed.py --reset
```

| Flag | Meaning |
|------|---------|
| `--reset` | drop existing tables and rebuild (required after the first run) |
| `--logs-only` | regenerate the query log + catalogue only, don't touch the DB |
| `--users N` | number of users (default 600; orders and tickets scale off this) |
| `--queries N` | approx query-log entries (default 30000) |
| `--days N` | query-log window (default 90) |
| `--seed N` | RNG seed (default 42) — same seed → same data |

## What you get

### Database `dma_auditor`

Three tables — `users`, `orders`, `support_tickets` — with a deliberate mix:

* **sensitive**: `email`, `phone`, `ssn`, `mothers_maiden_name`,
  `password_hash`, `shipping_address` / `billing_address`, `card_last4`
* **borderline** (PII-by-law but easy to overlook): `last_login_ip`,
  `orders.ip_address`, `date_of_birth`, `gender`, `support_tickets.body`
* **harmless**: `favorite_color`, `theme_preference`, `locale`,
  `customer_sentiment`, `satisfaction_rating`

Timestamps span ~2 years. ~18% of users (and a matching slice of orders /
tickets) are older than 400 days, so the retention checker has stale rows to
find. Support tickets skew oldest on purpose — support data is the classic
"never purged" case.

### `output/query_log.jsonl` and `output/query_log.csv`

~30k simulated queries over the last 90 days, one per line (JSONL) or row
(CSV). Each entry: timestamp, template name, service, DB role, operation,
tables, `columns_read`, `columns_written`, row count, duration, SQL sketch.
Columns are namespaced `table.column`.

Usage is uneven **by design**:

| Pattern | Examples |
|---|---|
| read constantly | `users.email`, `users.theme_preference`, `orders.status` |
| read rarely | `users.ssn`, `users.gender`, `orders.billing_address` |
| never read by any query | `users.mothers_maiden_name`, `users.favorite_color`, `orders.notes`, `support_tickets.satisfaction_rating` |

That "never read" set is the signal the Phase-2 Usage Analyzer must surface.

### `output/column_catalog.csv`

Ground truth for every column: intended sensitivity tier + score, PII type,
expected usage band, whether it was seeded with stale rows, and a one-line
rationale. Use it to score the classifier / analyzer / scoring engine in
later phases.

### `output/manifest.json`

Row counts, query-log window, seed — a summary of what the last run produced.

## Notes for later phases

* The auditor itself must connect **read-only** (see `../CLAUDE.md`). Create a
  separate role for it — this seeder's `dma_seed` role is write-capable and
  should not be reused by the auditor:

  ```sql
  CREATE ROLE dma_auditor_ro WITH LOGIN PASSWORD 'choose_me';
  GRANT CONNECT ON DATABASE dma_auditor TO dma_auditor_ro;
  \c dma_auditor
  GRANT USAGE ON SCHEMA public TO dma_auditor_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO dma_auditor_ro;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dma_auditor_ro;
  ```
