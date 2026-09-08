"""Evaluate columns/tables against a retention policy -> Staleness score.

    oldest_age  = now - min(anchor timestamp)          (from Phase 1 metadata)
    days_overdue = oldest_age - policy_days
    age_score    = clamp(days_overdue / policy_days, 0, 1)   # overdue by a
                                                             # full period -> 1.0
    score        = age_score                                  (metadata only)
                 = age_score * (0.4 + 0.6 * volume)           (with a live
                   connector: volume = min(1, fraction_overdue / 0.10))

Columns that carry their own record-lifecycle timestamp (``*_at``, ``created``,
``resolved`` ...) are scored on their own values (``basis = "self"``).
Every other column - including attribute dates like ``date_of_birth`` -
inherits its table's score via an anchor column (``basis = "table:created_at"``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from auditor.ingestion.metadata import ColumnMetadata, DatabaseMetadata, TableMetadata
from auditor.retention.policy import RetentionPolicy

VOLUME_TARGET = 0.10          # fraction of rows overdue for full "volume" weight

_ANCHOR_NAME = re.compile(
    r"(^|_)(created|inserted|added|registered|signed_?up|onboarded|"
    r"updated|modified|changed|touched|"
    r"deleted|removed|archived|purged|"
    r"resolved|closed|completed|finished|cancelled|"
    r"last_?login|last_?seen|last_?active|last_?access|accessed|"
    r"synced|processed|imported|exported|refreshed)(_|$)"
    r"|_(at|ts|time|on|datetime|timestamp)$|^(ts|timestamp|event_time)$",
    re.I,
)
_ATTRIBUTE_NAME = re.compile(
    r"(birth|dob|expir|valid_from|valid_to|valid_until|effective|"
    r"start_date|end_date|due_?date|scheduled|renew|anniversary|maturity|"
    r"as_of|period_)",
    re.I,
)


def is_retention_relevant(col: ColumnMetadata) -> bool:
    """True if this column is a record-lifecycle timestamp we can score on its
    own values (vs an attribute/business date that just happens to be temporal)."""
    if not col.is_temporal:
        return False
    name = col.name.lower()
    if _ATTRIBUTE_NAME.search(name):
        return False
    if _ANCHOR_NAME.search(name):
        return True
    return col.generic_type in ("timestamptz", "timestamp")


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(ts: Optional[datetime], now: datetime) -> Optional[float]:
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 86400)


def _age_score(oldest_age: Optional[float], policy_days: int) -> tuple[float, Optional[int]]:
    if oldest_age is None or policy_days <= 0:
        return 0.0, None
    overdue = oldest_age - policy_days
    if overdue <= 0:
        return 0.0, int(round(overdue))
    return round(min(1.0, overdue / policy_days), 4), int(round(overdue))


@dataclass
class ColumnRetention:
    qualified_name: str
    table: str
    column: str
    score: float                       # 0-1 staleness; feeds the Staleness term
    is_overdue: bool
    days_overdue: Optional[int]
    policy_applied: dict               # {"days": int, "source": str, "anchor": str|None}
    oldest_row_age_days: Optional[int]
    newest_row_age_days: Optional[int]
    basis: str                         # "self" | "table:<anchor>" | "excluded" | "no-temporal-data"
    rows_total: Optional[int] = None
    rows_overdue: Optional[int] = None
    fraction_overdue: Optional[float] = None
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TableRetention:
    name: str
    anchor: Optional[str]
    policy_days: int
    policy_source: str
    score: float
    is_overdue: bool
    days_overdue: Optional[int]
    oldest_row_age_days: Optional[int]
    newest_row_age_days: Optional[int]
    rows_total: Optional[int] = None
    rows_overdue: Optional[int] = None
    fraction_overdue: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetentionResult:
    columns: list[ColumnRetention]
    tables: list[TableRetention]
    policy: dict
    now: str
    used_connector: bool

    def column(self, qualified_name: str) -> Optional[ColumnRetention]:
        return next((c for c in self.columns if c.qualified_name == qualified_name), None)

    def table(self, name: str) -> Optional[TableRetention]:
        return next((t for t in self.tables if t.name == name), None)

    @property
    def by_name(self) -> dict[str, ColumnRetention]:
        return {c.qualified_name: c for c in self.columns}

    def to_dict(self) -> dict:
        return {
            "now": self.now,
            "used_connector": self.used_connector,
            "policy": self.policy,
            "tables": [t.to_dict() for t in self.tables],
            "columns": [c.to_dict() for c in self.columns],
        }

    def summary_table(self) -> str:
        rows = [f"{'column':<40} {'score':>6}  {'overdue?':<9} {'days over':>9}  basis"]
        rows.append("-" * 90)
        for c in sorted(self.columns, key=lambda x: (-x.score, x.qualified_name)):
            rows.append(
                f"{c.qualified_name:<40} {c.score:>6.3f}  "
                f"{'OVERDUE' if c.is_overdue else 'ok':<9} "
                f"{(c.days_overdue if c.days_overdue is not None else '-'):>9}  {c.basis}"
            )
        return "\n".join(rows)


class RetentionChecker:
    def __init__(
        self,
        policy: Optional[RetentionPolicy] = None,
        metadata: Optional[DatabaseMetadata] = None,
        *,
        connector=None,
        now: Optional[datetime] = None,
    ):
        self.policy = policy or RetentionPolicy()
        self.metadata = metadata
        self.connector = connector
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------ #

    def check(self, metadata: Optional[DatabaseMetadata] = None) -> RetentionResult:
        meta = metadata or self.metadata
        if meta is None:
            raise ValueError("RetentionChecker.check needs a DatabaseMetadata")

        live_stats = self._live_stats(meta) if self.connector else {}

        table_results: list[TableRetention] = []
        column_results: list[ColumnRetention] = []

        for tbl in meta.tables:
            anchor = self._pick_anchor(tbl)
            tr = self._table_retention(tbl, anchor, live_stats.get(tbl.name, {}))
            table_results.append(tr)
            for col in tbl.columns:
                column_results.append(
                    self._column_retention(tbl, col, anchor, tr, live_stats.get(tbl.name, {}))
                )

        column_results.sort(key=lambda c: (-c.score, c.qualified_name))
        return RetentionResult(
            columns=column_results, tables=table_results,
            policy=self.policy.to_dict(), now=self.now.isoformat(),
            used_connector=bool(self.connector),
        )

    # ------------------------------------------------------------------ #

    def _pick_anchor(self, tbl: TableMetadata) -> Optional[ColumnMetadata]:
        named = self.policy.anchor_for(tbl.name)
        if named:
            c = tbl.column(named)
            if c is not None:
                return c
        relevant = [c for c in tbl.columns if is_retention_relevant(c) and c.min_value]
        if not relevant:
            return None
        for pref in ("created_at", "created", "inserted_at", "registered_at",
                     "signed_up_at", "added_at", "onboarded_at"):
            for c in relevant:
                if c.name.lower() == pref:
                    return c
        # otherwise the column whose oldest value is the oldest (most conservative)
        datable = [(c, _parse_ts(c.min_value)) for c in relevant]
        datable = [(c, ts) for c, ts in datable if ts is not None]
        if not datable:
            return None
        return min(datable, key=lambda pair: pair[1])[0]

    def _table_retention(self, tbl, anchor, stats) -> TableRetention:
        if anchor is None:
            return TableRetention(
                name=tbl.name, anchor=None, policy_days=self.policy.days_for(tbl.name)[0],
                policy_source=self.policy.days_for(tbl.name)[1], score=0.0,
                is_overdue=False, days_overdue=None,
                oldest_row_age_days=None, newest_row_age_days=None,
                rows_total=tbl.row_count,
            )
        days, source = self.policy.days_for(tbl.name)
        oldest = _age_days(_parse_ts(anchor.min_value), self.now)
        newest = _age_days(_parse_ts(anchor.max_value), self.now)
        age_score, overdue = _age_score(oldest, days)
        frac = stats.get(anchor.name, {}).get("fraction")
        rows_over = stats.get(anchor.name, {}).get("overdue")
        score = self._blend(age_score, frac)
        return TableRetention(
            name=tbl.name, anchor=anchor.name, policy_days=days, policy_source=source,
            score=score, is_overdue=score > 0 or (overdue is not None and overdue > 0),
            days_overdue=overdue if (overdue and overdue > 0) else None,
            oldest_row_age_days=int(oldest) if oldest is not None else None,
            newest_row_age_days=int(newest) if newest is not None else None,
            rows_total=stats.get("_rows", tbl.row_count),
            rows_overdue=rows_over, fraction_overdue=frac,
        )

    def _column_retention(self, tbl, col, anchor, tr: TableRetention, stats) -> ColumnRetention:
        qn = col.qualified_name

        if self.policy.is_excluded(tbl.name, col.name):
            return ColumnRetention(
                qualified_name=qn, table=tbl.name, column=col.name,
                score=0.0, is_overdue=False, days_overdue=None,
                policy_applied={"days": None, "source": "excluded", "anchor": None},
                oldest_row_age_days=None, newest_row_age_days=None, basis="excluded",
                rows_total=tbl.row_count,
            )

        if is_retention_relevant(col) and col.min_value:
            days, source = self.policy.days_for(tbl.name, col.name)
            oldest = _age_days(_parse_ts(col.min_value), self.now)
            newest = _age_days(_parse_ts(col.max_value), self.now)
            age_score, overdue = _age_score(oldest, days)
            frac = stats.get(col.name, {}).get("fraction")
            rows_over = stats.get(col.name, {}).get("overdue")
            score = self._blend(age_score, frac)
            return ColumnRetention(
                qualified_name=qn, table=tbl.name, column=col.name,
                score=score,
                is_overdue=bool(overdue and overdue > 0),
                days_overdue=overdue if (overdue and overdue > 0) else None,
                policy_applied={"days": days, "source": source, "anchor": col.name},
                oldest_row_age_days=int(oldest) if oldest is not None else None,
                newest_row_age_days=int(newest) if newest is not None else None,
                basis="self",
                rows_total=stats.get("_rows", tbl.row_count),
                rows_overdue=rows_over, fraction_overdue=frac,
                signals={"age_score": age_score},
            )

        # inherit the table's staleness (non-temporal, or attribute date)
        if anchor is None:
            return ColumnRetention(
                qualified_name=qn, table=tbl.name, column=col.name,
                score=0.0, is_overdue=False, days_overdue=None,
                policy_applied={"days": tr.policy_days, "source": tr.policy_source, "anchor": None},
                oldest_row_age_days=None, newest_row_age_days=None,
                basis="no-temporal-data", rows_total=tbl.row_count,
            )
        return ColumnRetention(
            qualified_name=qn, table=tbl.name, column=col.name,
            score=tr.score, is_overdue=tr.is_overdue, days_overdue=tr.days_overdue,
            policy_applied={"days": tr.policy_days, "source": tr.policy_source, "anchor": tr.anchor},
            oldest_row_age_days=tr.oldest_row_age_days,
            newest_row_age_days=tr.newest_row_age_days,
            basis=f"table:{tr.anchor}",
            rows_total=tr.rows_total, rows_overdue=tr.rows_overdue,
            fraction_overdue=tr.fraction_overdue,
        )

    @staticmethod
    def _blend(age_score: float, fraction_overdue: Optional[float]) -> float:
        if age_score <= 0:
            return 0.0
        if fraction_overdue is None:
            return round(age_score, 4)
        volume = min(1.0, fraction_overdue / VOLUME_TARGET)
        return round(age_score * (0.4 + 0.6 * volume), 4)

    # ------------------------------------------------------------------ #

    def _live_stats(self, meta: DatabaseMetadata) -> dict:
        """One aggregate query per table: total rows + rows past policy for each
        retention-relevant temporal column."""
        from sqlalchemy import text

        out: dict[str, dict] = {}
        with self.connector.connect() as conn:
            for tbl in meta.tables:
                cols = [c for c in tbl.columns if is_retention_relevant(c) and c.min_value]
                if not cols:
                    continue
                prep = conn.dialect.identifier_preparer
                qt = f"{prep.quote_schema(tbl.schema)}.{prep.quote(tbl.name)}"
                selects = ["count(*) AS _rows"]
                params = {}
                for i, c in enumerate(cols):
                    days = self.policy.days_for(tbl.name, c.name)[0]
                    cutoff = self.now.timestamp() - days * 86400
                    params[f"c{i}"] = datetime.fromtimestamp(cutoff, tz=timezone.utc)
                    selects.append(
                        f"count(*) FILTER (WHERE {prep.quote(c.name)} < :c{i}) AS f{i}"
                    )
                row = conn.execute(text(f"SELECT {', '.join(selects)} FROM {qt}"), params).mappings().one()
                tstat = {"_rows": row["_rows"]}
                for i, c in enumerate(cols):
                    over = row[f"f{i}"]
                    tstat[c.name] = {
                        "overdue": int(over),
                        "fraction": round(over / row["_rows"], 4) if row["_rows"] else 0.0,
                    }
                out[tbl.name] = tstat
        return out


def check_retention(
    metadata: DatabaseMetadata,
    policy: Optional[RetentionPolicy] = None,
    *,
    connector=None,
    now: Optional[datetime] = None,
) -> RetentionResult:
    return RetentionChecker(policy, metadata, connector=connector, now=now).check()
