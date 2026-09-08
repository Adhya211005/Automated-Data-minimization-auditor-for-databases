"""Audit-history persistence (SQLite).

Every audit run and its per-column findings are stored so a DPO can compare
runs over time - "this column was flagged last month too", "this one just
crossed the threshold".
"""

from auditor.storage.db import get_session, init_db, reset_engine
from auditor.storage.models import AuditRunRow, ColumnFindingRow
from auditor.storage.repository import (
    RunNotFound,
    diff_runs,
    get_run,
    list_runs,
    record_run,
    run_findings,
)

__all__ = [
    "init_db",
    "get_session",
    "reset_engine",
    "AuditRunRow",
    "ColumnFindingRow",
    "record_run",
    "get_run",
    "list_runs",
    "run_findings",
    "diff_runs",
    "RunNotFound",
]
