"""Run the full audit pipeline: ingest -> sensitivity + usage + retention -> score.

One entry point (`run_audit`) used by both the CLI (`python -m auditor.scoring`)
and the API (`auditor.api`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from auditor.ingestion.metadata import DatabaseMetadata
from auditor.retention.checker import RetentionChecker
from auditor.retention.policy import RetentionPolicy
from auditor.scoring.engine import ScoringEngine, ScoringResult
from auditor.sensitivity.classifier import RegexSensitivityClassifier
from auditor.usage.analyzer import UsageAnalyzer

DEFAULT_LOG = "seed-data/output/query_log.jsonl"


@dataclass
class AuditRunConfig:
    metadata_path: Optional[str] = None      # None -> extract from the live target DB
    log_path: str = DEFAULT_LOG
    policy_path: Optional[str] = None
    policy_days: Optional[int] = None         # overrides default_days
    window_days: int = 90
    mode: str = "hybrid"
    live_retention: bool = False
    now: Optional[datetime] = None            # fixed clock (tests / reproducibility)

    def resolved_policy(self) -> RetentionPolicy:
        p = RetentionPolicy.from_file(self.policy_path) if self.policy_path else RetentionPolicy()
        if self.policy_days is not None:
            p.default_days = self.policy_days
        return p


@dataclass
class AuditRun:
    result: ScoringResult
    config: AuditRunConfig
    policy: RetentionPolicy
    metadata: DatabaseMetadata
    target: str
    started_at: datetime
    finished_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def _load_metadata(path: Optional[str]) -> DatabaseMetadata:
    if path:
        return DatabaseMetadata.load(path)
    from auditor.ingestion import extract_metadata
    return extract_metadata()


def run_audit(config: Optional[AuditRunConfig] = None, **overrides) -> AuditRun:
    cfg = config or AuditRunConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)

    started = datetime.now(timezone.utc)
    meta = _load_metadata(cfg.metadata_path)

    sensitivities = RegexSensitivityClassifier().classify_all(meta.iter_columns())

    usages = UsageAnalyzer(
        meta, window_days=cfg.window_days, mode=cfg.mode, now=cfg.now,
    ).analyze(cfg.log_path).columns

    connector = None
    if cfg.live_retention:
        from auditor.ingestion.connector import ReadOnlyConnector, ReadOnlyViolation
        try:
            connector = ReadOnlyConnector.from_env(strict=False)
        except ReadOnlyViolation:
            connector = None

    policy = cfg.resolved_policy()
    retentions = RetentionChecker(
        policy, meta, connector=connector, now=cfg.now,
    ).check().columns

    result = ScoringEngine().score_all(sensitivities, usages, retentions)
    finished = datetime.now(timezone.utc)

    return AuditRun(
        result=result, config=cfg, policy=policy, metadata=meta,
        target=f"{meta.dialect}://{meta.database}/{meta.schema}",
        started_at=started, finished_at=finished,
    )
