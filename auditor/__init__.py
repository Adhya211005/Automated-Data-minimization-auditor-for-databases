"""Automated Data Minimization Auditor.

Phase 1 provides the ingestion layer only: a read-only DB connector and a
metadata extractor. The analysis engines (sensitivity, usage, retention) and
scoring are added in later phases and consume this layer's output.
"""

__version__ = "0.1.0"
