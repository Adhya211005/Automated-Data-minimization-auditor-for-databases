"""FastAPI backend for the Data Minimization Auditor (Phase 6).

Read-oriented: it triggers audit runs and serves their results + history.
No endpoint mutates the target database or the findings - remediation
(archive / DROP scripts) is a later phase.

    uvicorn auditor.api.main:app --reload
"""

from auditor.api.main import app

__all__ = ["app"]
