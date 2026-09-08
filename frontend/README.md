# Dashboard (Phase 7)

React + Vite single-page UI over the Phase 6 API. Single-operator demo tool —
no auth, no routing library, functional over polished.

## Run

```powershell
# 1. backend (from the repo root)
uvicorn auditor.api.main:app --port 8000
#    seed it with a run or two:
#    curl -X POST http://127.0.0.1:8000/audits -H "content-type: application/json" -d "{\"policy_days\":365,\"note\":\"baseline\"}"

# 2. dashboard
cd frontend
npm install
npm run dev            # http://localhost:5173
```

`npm run dev` proxies `/api/*` → `http://127.0.0.1:8000` (see `vite.config.js`;
override with `VITE_API_TARGET`). For a built/preview deploy the API also
sends permissive CORS for `localhost:5173/4173`.

## What it does

| view | backing endpoint | shows |
|------|------------------|-------|
| **Report** (default) | `GET /audits/{id}` | the selected run's ranked table — flagged first, verdict badge, per-column mini-bars for sensitive / (1−usage) / stale, and the top reason. "flagged only" toggle. Run picker in the header. |
| **Column detail** (click a row) | `GET /audits/{id}/columns/{name}` | the necessity formula spelled out, the three `SignalBar`s with their thresholds, then the raw evidence: PII type + matched rules (chips), read/write counts + access-by-service + query templates, days overdue + policy + basis. For flagged/review columns, a **"Suggested fix"** block — the draft SQL migration with a **Copy SQL** button (draft only, the UI can't run it). Plus a mini history table (`GET /columns/{name}/history`). This is the "*why*, not just *that*" panel. |
| **Compare runs** | `GET /audits/compare` | pick baseline → current; surfaces **newly flagged**, **no longer flagged**, **verdict changed** (with from→to badges), and score movement. Every column links back into the detail panel. |
| **+ New audit** | `POST /audits` | policy days / window / attribution mode / note → runs the pipeline, persists, selects the new run. |

## Layout

```
src/
  api.js              fetch wrappers for the 6 endpoints
  format.js           verdict labels + colors, date/number helpers
  App.jsx             view state, run selection, wiring
  components/
    RunSummaryBar.jsx   flagged count, thresholds, verdict tallies
    ReportTable.jsx     the ranked column table
    ColumnDetail.jsx    the evidence panel  (the demo centrepiece)
    SuggestedFix.jsx    the draft SQL + Copy button
    SignalBar.jsx       one 0-1 signal + threshold marker
    CompareView.jsx     run-to-run diff
    NewAuditForm.jsx    POST /audits modal
```

No state library, no router — `useState` in `App.jsx`, props down.
