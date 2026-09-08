"""SQLAlchemy models for audit history."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditRunRow(Base):
    __tablename__ = "audit_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    target: Mapped[str] = mapped_column(String(255), default="")
    window_days: Mapped[int] = mapped_column(Integer, default=90)
    mode: Mapped[str] = mapped_column(String(16), default="hybrid")
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict)
    n_columns: Mapped[int] = mapped_column(Integer, default=0)
    n_flagged: Mapped[int] = mapped_column(Integer, default=0)
    verdict_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")

    findings: Mapped[list["ColumnFindingRow"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ColumnFindingRow.rank",
    )


class ColumnFindingRow(Base):
    __tablename__ = "column_finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("audit_run.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)

    qualified_name: Mapped[str] = mapped_column(String(255), index=True)
    table_name: Mapped[str] = mapped_column(String(128))
    column_name: Mapped[str] = mapped_column(String(128))

    score: Mapped[float] = mapped_column(Float, default=0.0)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict: Mapped[str] = mapped_column(String(32), default="")

    sensitivity: Mapped[float] = mapped_column(Float, default=0.0)
    usage: Mapped[float] = mapped_column(Float, default=0.0)
    retention: Mapped[float] = mapped_column(Float, default=0.0)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    is_unused: Mapped[bool] = mapped_column(Boolean, default=False)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)

    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped["AuditRunRow"] = relationship(back_populates="findings")
