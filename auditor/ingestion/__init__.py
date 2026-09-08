"""Ingestion layer - read-only connector + metadata extractor (Phase 1).

The "eyes" of the system: everything downstream (sensitivity classifier,
usage analyzer, retention checker) consumes ``DatabaseMetadata`` produced
here, and nothing else in the auditor is allowed to open its own connection
to a target database.
"""

from auditor.ingestion.connector import (
    ReadOnlyConnector,
    ReadOnlyReport,
    ReadOnlyViolation,
    WriteAttemptBlocked,
)
from auditor.ingestion.metadata import (
    ColumnMetadata,
    DatabaseMetadata,
    MetadataExtractor,
    TableMetadata,
    extract_metadata,
)

__all__ = [
    "ReadOnlyConnector",
    "ReadOnlyReport",
    "ReadOnlyViolation",
    "WriteAttemptBlocked",
    "ColumnMetadata",
    "TableMetadata",
    "DatabaseMetadata",
    "MetadataExtractor",
    "extract_metadata",
]
