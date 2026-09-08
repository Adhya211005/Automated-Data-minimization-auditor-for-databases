"""Retention policy engine (Phase 4).

Answers "is this data overdue?" - evaluates temporal columns against a
configurable per-table / per-column day-threshold policy and produces a
**Staleness score** (0-1) per column.

``ColumnRetention`` is the third signal, parallel to ``ColumnSensitivity``
(Phase 2) and ``ColumnUsage`` (Phase 3). It feeds the ``Staleness`` term of
``Necessity = Sensitivity x (1 - Usage) x Staleness``.

Staleness is really a row-lifecycle property, so columns that don't carry
their own audit timestamp inherit their table's score (computed from an
anchor column, ``created_at`` by default).
"""

from auditor.retention.checker import (
    ColumnRetention,
    RetentionChecker,
    RetentionResult,
    TableRetention,
    check_retention,
    is_retention_relevant,
)
from auditor.retention.policy import RetentionPolicy

__all__ = [
    "RetentionPolicy",
    "RetentionChecker",
    "ColumnRetention",
    "TableRetention",
    "RetentionResult",
    "check_retention",
    "is_retention_relevant",
]
