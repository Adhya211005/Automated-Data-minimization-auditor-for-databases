# Database migrations

Plain `.sql` files, numbered, run in order with `psql`. These set up the
**roles and grants** the auditor needs on a target database — they do not
touch application schema (that's the seeder's job in Phase 0).

| File | Purpose | Run as |
|------|---------|--------|
| `001_create_readonly_role.sql` | Create `dma_auditor_ro` — a login-only, SELECT-only role scoped to the `dma_auditor` database. This is how the "auditor never writes" rule (CLAUDE.md) is actually enforced. | superuser (`postgres`) |

## Running

```powershell
$env:Path += ";C:\Program Files\PostgreSQL\18\bin"
psql -U postgres -f migrations/001_create_readonly_role.sql
```

All files are idempotent — safe to re-run (e.g. after `seed.py --reset`
creates new tables, re-run `001` to grant SELECT on them; the
`ALTER DEFAULT PRIVILEGES` line covers tables created by `dma_seed` from then
on).

## Credentials

`001` creates `dma_auditor_ro` with password `dma_auditor_ro_pw`. The auditor
reads this from `auditor/.env` (gitignored; see `auditor/.env.example`).
Change it in both places if you want something else.

## Why a dedicated role and not just a read-only transaction?

Defence in depth. The connector (`auditor/ingestion/connector.py`) also forces
`default_transaction_read_only = on` and refuses to run non-SELECT SQL — but
the role is the guarantee that survives a bug in that code: even if the
connector tried to issue an `INSERT`, PostgreSQL rejects it because
`dma_auditor_ro` holds no such privilege.
