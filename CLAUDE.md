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