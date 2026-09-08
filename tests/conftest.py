"""Shared fixtures.

The ingestion tests need the Phase 0 seed database plus the read-only role
from migrations/001. If either is missing the tests skip (with a message
telling you what to run) rather than fail.
"""

from __future__ import annotations

import pytest

from auditor.config import TargetConfig
from auditor.ingestion.connector import ReadOnlyConnector, ReadOnlyViolation


@pytest.fixture(scope="session")
def target_config() -> TargetConfig:
    return TargetConfig.from_env()


@pytest.fixture(scope="session")
def connector(target_config: TargetConfig) -> ReadOnlyConnector:
    try:
        conn = ReadOnlyConnector(target_config, strict=False)
    except ReadOnlyViolation as exc:
        pytest.skip(
            f"cannot reach target DB as {target_config.user}: {exc}\n"
            "Run:  psql -U postgres -f migrations/001_create_readonly_role.sql\n"
            "and:  python seed-data/seed.py --reset"
        )
    yield conn
    conn.dispose()


@pytest.fixture(scope="session")
def metadata(connector: ReadOnlyConnector):
    from auditor.ingestion.metadata import MetadataExtractor

    if not connector.report.ok:
        pytest.skip("connection is not read-only; fix migrations/001 first")
    return MetadataExtractor(connector).extract(sample_rows=40, max_samples=12)
