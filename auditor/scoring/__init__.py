"""Necessity scoring / correlation engine (Phase 5) - the "brain".

Combines the three independent signals into one decision per column:

    Necessity = Sensitivity x (1 - Usage) x Staleness

but a column is only **flagged** when all three cross their own threshold
(sensitive AND unused AND stale), not merely when the product is large.

Consumes ``ColumnSensitivity`` (Phase 2), ``ColumnUsage`` (Phase 3) and
``ColumnRetention`` (Phase 4); produces ``NecessityScore``.
"""

from auditor.scoring.engine import (
    THRESHOLDS,
    NecessityScore,
    ScoringEngine,
    ScoringResult,
    score_database,
)

__all__ = [
    "NecessityScore",
    "ScoringEngine",
    "ScoringResult",
    "THRESHOLDS",
    "score_database",
]
