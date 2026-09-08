# `auditor/` — the audit engine

Phase 1 delivers the **ingestion layer** only. Later phases add
`sensitivity/`, `usage/`, `retention/`, `scoring/`, `api/`.

```
auditor/
  config.py              TargetConfig — where the scanned DB is, read from auditor/.env
  ingestion/
    connector.py         ReadOnlyConnector — verified read-only handle to a target DB
    metadata.py          MetadataExtractor -> DatabaseMetadata (the downstream contract)
    __main__.py          CLI: python -m auditor.ingestion
```

## Setup

```powershell
pip install -r requirements.txt

# read-only role on the Phase 0 seed DB (run once, as superuser)
$env:Path += ";C:\Program Files\PostgreSQL\18\bin"
psql -U postgres -f migrations/001_create_readonly_role.sql

copy auditor\.env.example auditor\.env
```

## Use

```powershell
python -m auditor.ingestion --check          # verify the connection cannot write
python -m auditor.ingestion                   # extract + print a schema summary
python -m auditor.ingestion --out schema.metadata.json
python -m auditor.ingestion --json | ...      # full document to stdout
```

From Python:

```python
from auditor.ingestion import extract_metadata

meta = extract_metadata()                      # builds a ReadOnlyConnector from env
col = meta.column("users.mothers_maiden_name")
print(col.generic_type, col.sample_values, col.is_temporal)
```

## The read-only guarantee

`ReadOnlyConnector.__init__` refuses to return (raises `ReadOnlyViolation`)
unless **all** of these hold, checked against the live connection:

| check | how |
|---|---|
| role is not a superuser | `current_setting('is_superuser')` |
| role cannot `CREATE` in the DB | `has_database_privilege(..., 'CREATE')` |
| role holds no INSERT/UPDATE/DELETE/TRUNCATE on any table | `has_table_privilege` sweep |
| session is `transaction_read_only = on` | `current_setting(...)` |
| an actual write is refused by the DB | raw-driver `CREATE TEMP TABLE` probe, must fail |

On top of that, a SQLAlchemy `before_cursor_execute` hook raises
`WriteAttemptBlocked` if any auditor code sends a statement that isn't a read,
and `connect()` always rolls its transaction back. Three layers; the role is
the one that still holds if the other two have bugs.

`tests/test_connector.py` exercises all of this, including a raw-cursor write
that bypasses the Python guard to confirm PostgreSQL itself says no.

## `DatabaseMetadata` — the contract for Phases 2–4

One object per extraction. Save it (`meta.save(path)`) and later phases can
run entirely offline from the JSON (`DatabaseMetadata.load(path)`), which is
how each analysis engine stays independently testable (CLAUDE.md).

- `meta.tables` → `TableMetadata` (row_count, primary_key, foreign_keys,
  `temporal_columns`, `columns`)
- `meta.column("table.col")` / `meta.iter_columns()` → `ColumnMetadata`
- `ColumnMetadata` fields and their intended consumer are documented at the
  top of `metadata.py`. Highlights:
  - `generic_type` — normalized family (`string`, `timestamptz`, `inet`, …)
  - `sample_values` — up to N distinct non-null values, JSON-safe (the main
    signal for the sensitivity classifier)
  - `is_temporal` + `min_value`/`max_value` — pre-fetched span for the
    retention checker
  - `is_primary_key` / `is_foreign_key` / `foreign_key_target` — for the
    usage analyzer

Sample values and row counts are **exact** here because the seed DB is small.
For a large production target, swap `_count_rows` for a `reltuples` estimate
and `ORDER BY random()` for `TABLESAMPLE` — the output shape doesn't change.
