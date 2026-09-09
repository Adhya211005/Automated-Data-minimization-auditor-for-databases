# Project: Automated Data Minimization Auditor

Cybersecurity coursework project (BCSE410L). Full spec in docs/project-spec.md — read it before making changes.

## Core concept
Flags database columns as minimization violations only when they are
sensitive AND unused AND stale (all three, not just one).
Necessity Score = Sensitivity × (1 − Usage) × Staleness

## Stack
- Backend: FastAPI (Python)
- DB targets: PostgreSQL / MySQL via SQLAlchemy
- Classification: scikit-learn (start with regex baseline first)
- Frontend: React
- Audit history storage: SQLite/Postgres
- Optional: Groq API for human-readable remediation text

## Conventions
- All DB connections must be read-only — never let the auditor write to
  the target database it's scanning.
- Keep modules independent: sensitivity classifier, usage analyzer, and
  retention checker must each be testable in isolation before scoring
  combines them.
- Write unit tests for the scoring edge cases explicitly (sensitive+used,
  sensitive+unused+fresh, sensitive+unused+stale).

  ## Status
Phase 0-4 done. All three signals built: sensitivity (regex, P/R
1.0/1.0), usage (sqlglot parser, Spearman 0.957 vs log truth),
retention (row-anchor inheritance for non-timestamp columns, lifecycle
vs attribute-date distinction). Spec anchors confirmed independently
in each: mothers_maiden_name unused+overdue, email used, dob not
falsely flagged. Next: Phase 5 — scoring engine, the integration
point. Formula: Sensitivity × (1 − Usage) × Staleness.

## Status
Phase 0-5 done. Full analysis pipeline complete: sensitivity + usage +
retention → NecessityScore with AND-gate flagging (not score cutoff).
Verified end-to-end on seed: 2/46 columns flagged (mothers_maiden_name,
ssn), all 4 spec edge cases pass, policy-sensitivity confirmed (longer
retention window drops both to "review"). Next: Phase 6 — FastAPI
backend + audit-history persistence, then Phase 7 dashboard.
## Status
Phase 0-6 done. Full pipeline: ingestion → sensitivity + usage +
retention → scoring (AND-gate) → FastAPI + SQLite persistence with
run comparison/history. Verified: reruns on unchanged data are
byte-identical; threshold changes correctly flip verdicts and are
caught by /audits/compare. Live DB path (not just seed metadata)
confirmed working end-to-end. Next: Phase 7 — React dashboard, the
last build phase.

## Status
Phase 0-7 done — all build phases complete. React+Vite dashboard
(frontend/) over the Phase 6 API: ranked Report view (flagged first,
per-column signal bars), Column detail panel (full evidence — matched
rules, read counts, days overdue — the "why not just that" view),
Compare view (newly_flagged / verdict_changed between runs), and
POST /audits trigger. Verified rendering against the live stack via
headless Chrome. Vite dev-proxies /api → :8000; API also sends CORS
for localhost. 161 backend tests pass. Next: integration / demo prep.

## Status
Remediation generator done (auditor/remediation/). For each flagged
(or 'review') column, drafts a SQL migration — archive-then-drop for
plain columns, anonymize-in-place for keys — with the necessity
evidence embedded as SQL comments and context-aware cautions
(statutory retention for govt_id/financial, row-level-delete
alternative when only some rows are overdue). DRAFT ONLY: never
connects to a DB, never executes; the row-level DELETE stays
commented out. Stored per-finding at audit time; served via
GET /audits/{id}/columns/{name}/remediation. Dashboard shows it as a
"Suggested fix" block with a Copy SQL button. 176 tests pass.
Next: integration / demo prep.

## Status
ML sensitivity classifier done (auditor/sensitivity/ml_classifier.py,
features.py, synth.py, evaluation_ml.py). scikit-learn logreg (default;
RF option) predicting the 4 tiers + a pii_type model, trained on ~900
synthetic columns labelled by the regex classifier. HONEST LIMITATION
(in README): labels come from the rules, so the model approximates the
rules rather than learning independent ground truth. Regex baseline
UNCHANGED and still the pipeline default. Results: logreg = 1.0 on
held-out synthetic, matches regex P/R/F1=1.0 on the seed vs
column_catalog.csv; RF underperforms (R 0.857). HybridSensitivityClassifier
(default mode 'blend', 0.6 regex + 0.4 ml, flags regex/ml disagreement)
= P/R/F1 1.0 + tier-acc 0.826 > regex 0.804. Usage-as-feature: no
effect, off by default. 193 tests pass. Next: integration / demo prep.