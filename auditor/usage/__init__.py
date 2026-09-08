"""Usage analyzer (Phase 3) - the project's core novelty.

Reads an application query log, attributes each query to the columns it
touches (projection / WHERE / JOIN / GROUP BY / ORDER BY, plus INSERT column
lists and UPDATE SET targets), and computes a per-column **Usage score**
(0-1) over a configurable time window.

The score feeds the ``(1 - Usage)`` term of the Necessity Score:
``Necessity = Sensitivity x (1 - Usage) x Staleness``. A column no query has
touched scores 0 -> ``(1 - 0) = 1`` -> maximum minimization pressure.

Output ``ColumnUsage`` is structurally parallel to Phase 2's
``ColumnSensitivity``: ``score`` / ``is_used`` / ``tier`` plus the evidence
(counts, last-access timestamps, read-vs-write split, per-service breakdown).
"""

from auditor.usage.analyzer import (
    ColumnUsage,
    UsageAnalyzer,
    UsageResult,
    analyze_log,
)
from auditor.usage.parser import QueryColumns, QueryParser
from auditor.usage.logsource import LogEntry, read_query_log

__all__ = [
    "ColumnUsage",
    "UsageAnalyzer",
    "UsageResult",
    "analyze_log",
    "QueryColumns",
    "QueryParser",
    "LogEntry",
    "read_query_log",
]
