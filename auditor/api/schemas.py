"""Request / response models for the API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AuditRequest(BaseModel):
    window_days: int = Field(90, ge=1, le=3650)
    mode: str = Field("hybrid", pattern="^(sql|declared|hybrid)$")
    policy_days: Optional[int] = Field(None, ge=1, description="override default retention days")
    policy_path: Optional[str] = Field(None, description="server-side path to a policy YAML/JSON")
    live_retention: bool = False
    note: str = ""
    # normally omitted -> audit the live target DB; set for offline / test runs
    metadata_path: Optional[str] = None
    log_path: Optional[str] = None


class RunSummary(BaseModel):
    id: str
    started_at: Optional[str]
    finished_at: Optional[str]
    target: str
    window_days: int
    mode: str
    policy: dict
    thresholds: dict
    n_columns: int
    n_flagged: int
    verdict_counts: dict
    duration_seconds: float
    note: str = ""


class Remediation(BaseModel):
    qualified_name: str
    strategy: str            # archive_then_drop | anonymize_in_place | none
    summary: str
    sql: str
    cautions: list[str]
    reversible: bool
    dialect: str


class Finding(BaseModel):
    rank: int
    qualified_name: str
    table: str
    column: str
    score: float
    is_flagged: bool
    verdict: str
    sensitivity: float
    usage: float
    retention: float
    is_sensitive: bool
    is_unused: bool
    is_stale: bool
    breakdown: dict
    reasons: list[str]
    has_remediation: bool = False


class ColumnEvidence(Finding):
    evidence: dict
    remediation: Optional[Remediation] = None


class RunReport(RunSummary):
    columns: list[Finding]


class HistoryEntry(BaseModel):
    run_id: str
    finished_at: Optional[str]
    score: float
    is_flagged: bool
    verdict: str
    sensitivity: float
    usage: float
    retention: float


class RunDiff(BaseModel):
    base: RunSummary
    head: RunSummary
    n_flagged_delta: int
    newly_flagged: list[dict[str, Any]]
    no_longer_flagged: list[dict[str, Any]]
    verdict_changed: list[dict[str, Any]]
    score_moved: list[dict[str, Any]]
