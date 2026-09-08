# `auditor/api/` + `auditor/storage/` — API & audit history (Phase 6)

Wraps the Phase 5 pipeline behind FastAPI and persists every run to SQLite so
a DPO can compare audits over time.

```
auditor/scoring/pipeline.py   run_audit() — the shared pipeline entry point
auditor/storage/models.py     audit_run, column_finding  (SQLAlchemy)
auditor/storage/db.py         SQLite engine ($AUDIT_HISTORY_DB or auditor/audit_history.db)
auditor/storage/repository.py record_run / get_run / list_runs / column_history / diff_runs
auditor/api/main.py           the FastAPI app
```

## Run

```powershell
uvicorn auditor.api.main:app --reload        # http://127.0.0.1:8000/docs
```

## Endpoints (all read-oriented — nothing mutates target data or findings)

| method + path | purpose |
|---|---|
| `POST /audits` | **trigger a fresh audit run**, persist it, return the summary. Body (all optional): `window_days`, `mode`, `policy_days`, `policy_path`, `live_retention`, `note`, `metadata_path`, `log_path`. Omit `metadata_path` to audit the live target DB. |
| `GET /audits` | list past runs, newest first (`?limit=`) |
| `GET /audits/{run_id}` | **one run's ranked report** — flagged columns first, full `breakdown` + `reasons` per column. `run_id` accepts `"latest"`. `?flagged_only=true` trims to the flagged set. |
| `GET /audits/{run_id}/columns/{qualified_name}` | **one column's full evidence** — `breakdown`, `reasons`, `evidence`, and (for flagged/review columns) the `remediation` draft |
| `GET /audits/{run_id}/columns/{qualified_name}/remediation` | just the draft SQL fix (`strategy`, `sql`, `cautions`, …); 404 for `keep` / `not_a_risk` |
| `GET /audits/compare?base=&head=` | what changed between two runs — `newly_flagged`, `no_longer_flagged`, `verdict_changed`, `score_moved` |
| `GET /columns/{qualified_name}/history` | one column across every run — "was this flagged last month too?" |
| `GET /health` | liveness + run count |

## Persistence — SQLite (`docs/project-spec.md` audit-history stack)

`audit_run` (id, timestamps, target, policy, thresholds, counts, verdict
breakdown) → `column_finding` (one row per column per run: the three
sub-scores, the booleans, `breakdown` / `reasons` / `evidence` as JSON).

`$AUDIT_HISTORY_DB` overrides the path (`:memory:` supported via a shared
pool; tests use a temp file).

## Why history matters

A real DPO audit is periodic, not one-shot:

- `GET /columns/users.ssn/history` → `[{run, score, is_flagged, verdict}, …]`
  — spot a column that's been flagged for three months running.
- `GET /audits/compare?base=<last month>&head=<today>` →
  `newly_flagged: [...]` catches a column that **just crossed the threshold**
  (schema change, a query got removed, retention clock ticked past policy).

## Determinism / comparability

Re-running against unchanged data gives an **identical** result: sensitivity
is pure regex, usage keys its clock off the log's last timestamp, and on the
seed every table is already ≥2× past policy so staleness clamps to 1.0.
`test_api.py::test_second_run_on_unchanged_data_is_identical` asserts every
column's `score` / `verdict` / `is_flagged` matches across two runs and that
`compare` reports zero deltas.

## Tests — `tests/test_api.py`

`TestClient`, fresh temp history DB per test, pipeline run offline from a
metadata dump + the Phase 0 log. Covers all three required endpoints, the
history + compare endpoints, 404s, and the consistency guarantee.
