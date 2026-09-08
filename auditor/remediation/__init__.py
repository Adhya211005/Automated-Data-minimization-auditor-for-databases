"""Remediation script generator (spec module 6 - "the hands").

For a flagged column, drafts a **SQL migration** that actually removes the
minimization violation: archive the data, then drop the column (or, when the
column can't be dropped, anonymize it in place). The column's evidence is
embedded as a SQL comment so a reviewer sees *why* before running anything.

DRAFT ONLY. This module never connects to a database and never executes SQL.
"""

from auditor.remediation.generator import (
    ColumnRemediation,
    RemediationGenerator,
    remediation_for_finding,
)

__all__ = [
    "ColumnRemediation",
    "RemediationGenerator",
    "remediation_for_finding",
]
