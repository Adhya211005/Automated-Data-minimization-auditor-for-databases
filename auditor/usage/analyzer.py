"""Turn a query log into a per-column Usage score.

For every column (from ``metadata`` if given, else every column seen in the
log) the analyzer counts reads and writes inside the time window, records the
last-access timestamps and the read/write + per-service breakdown, and folds
frequency and recency into a single 0-1 ``score``.

score
-----
    weighted  = reads + WRITE_WEIGHT * writes             reads dominate - "is
                this column still needed" is really about reads
    rank      = percentile rank of `weighted` among ALL columns (0-usage
                columns included) -> spreads the score across the schema so
                the dashboard can rank "least used first"
    absolute  = log1p(weighted) / log1p(ABS_REFERENCE)    keeps a genuinely
                busy database from looking the same as a quiet one
    freq      = RANK_W * rank + (1 - RANK_W) * absolute
    recency   = 0.5 ** (days_since_last_access / half_life)
    score     = 0                                      if never accessed
              = freq * (RECENCY_FLOOR + (1-RECENCY_FLOOR) * recency)

``is_used`` is the plain binary "did any query in the window touch it" - that
is the unambiguous signal the never-queried columns are meant to trip.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from auditor.ingestion.metadata import DatabaseMetadata
from auditor.usage.logsource import LogEntry, read_query_log
from auditor.usage.parser import QueryColumns, QueryParser

WRITE_WEIGHT = 0.25       # a write counts for less than a read
RANK_W = 0.55             # blend of "rank within this schema" vs "absolute rate"
ABS_REFERENCE = 400.0     # weighted accesses over the window that count as "clearly in use"
RECENCY_FLOOR = 0.50      # recency can at most halve a column's score

_TIERS = ((1e-9, "unused"), (0.20, "rare"), (0.45, "occasional"), (0.75, "frequent"), (1.01, "heavy"))


def _tier(score: float) -> str:
    for hi, name in _TIERS:
        if score < hi:
            return name
    return "heavy"


@dataclass
class ColumnUsage:
    qualified_name: str
    table: str
    column: str
    score: float                       # 0-1 usage; feeds (1 - score)
    is_used: bool                      # any access in the window
    tier: str                          # unused | rare | occasional | frequent | heavy
    read_count: int
    write_count: int
    total_count: int
    last_read_at: Optional[str]
    last_write_at: Optional[str]
    last_access_at: Optional[str]
    first_access_at: Optional[str]
    distinct_days: int
    distinct_services: int
    access_by_type: dict = field(default_factory=dict)      # {"read": n, "write": m}
    access_by_service: dict = field(default_factory=dict)   # {"web-app": n, ...}
    query_templates: list[str] = field(default_factory=list)
    window_days: int = 90
    attribution: str = "none"          # sqlglot | declared | hybrid | none
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Acc:
    reads: int = 0
    writes: int = 0
    last_read: Optional[datetime] = None
    last_write: Optional[datetime] = None
    first: Optional[datetime] = None
    days: set = field(default_factory=set)
    services: dict = field(default_factory=dict)
    templates: set = field(default_factory=set)
    from_sql: int = 0
    from_declared: int = 0


@dataclass
class UsageResult:
    columns: list[ColumnUsage]
    window_days: int
    window_start: str
    window_end: str
    n_queries: int
    n_in_window: int
    n_parsed_sql: int
    n_fallback_declared: int
    n_unparseable: int
    mode: str

    def column(self, qualified_name: str) -> Optional[ColumnUsage]:
        return next((c for c in self.columns if c.qualified_name == qualified_name), None)

    @property
    def by_name(self) -> dict[str, ColumnUsage]:
        return {c.qualified_name: c for c in self.columns}

    def to_dict(self) -> dict:
        return {
            "window_days": self.window_days,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "mode": self.mode,
            "n_queries": self.n_queries,
            "n_in_window": self.n_in_window,
            "n_parsed_sql": self.n_parsed_sql,
            "n_fallback_declared": self.n_fallback_declared,
            "n_unparseable": self.n_unparseable,
            "columns": [c.to_dict() for c in self.columns],
        }

    def summary_table(self) -> str:
        rows = [f"{'column':<40} {'score':>6} {'tier':<11} {'reads':>8} {'writes':>8}  last access"]
        rows.append("-" * 92)
        for c in sorted(self.columns, key=lambda x: (-x.score, x.qualified_name)):
            rows.append(
                f"{c.qualified_name:<40} {c.score:>6.3f} {c.tier:<11} "
                f"{c.read_count:>8,} {c.write_count:>8,}  {(c.last_access_at or '-')[:10]}"
            )
        return "\n".join(rows)


class UsageAnalyzer:
    def __init__(
        self,
        metadata: Optional[DatabaseMetadata] = None,
        *,
        window_days: int = 90,
        now: Optional[datetime] = None,
        mode: str = "hybrid",              # "sql" | "declared" | "hybrid"
        half_life_days: Optional[float] = None,
        dialect: str = "postgres",
    ):
        if mode not in ("sql", "declared", "hybrid"):
            raise ValueError(f"mode must be sql|declared|hybrid, got {mode!r}")
        self.metadata = metadata
        self.window_days = window_days
        self.now = now
        self.mode = mode
        self.half_life = half_life_days or (window_days / 3)
        self.parser = QueryParser(metadata, dialect=dialect)

    # ------------------------------------------------------------------ #

    def analyze(self, log: str | Path | Iterable[LogEntry]) -> UsageResult:
        entries = read_query_log(log) if isinstance(log, (str, Path)) else log
        entries = list(entries)

        now = self.now or max((e.ts for e in entries), default=datetime.now(timezone.utc))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - timedelta(days=self.window_days)

        acc: dict[str, _Acc] = {}
        n_in_window = n_parsed = n_fallback = n_unparseable = 0

        for e in entries:
            ts = e.ts if e.ts.tzinfo else e.ts.replace(tzinfo=timezone.utc)
            if ts < cutoff or ts > now:
                continue
            n_in_window += 1

            reads, writes, attribution = self._columns_for(e)
            if attribution == "sqlglot":
                n_parsed += 1
            elif attribution == "declared":
                n_fallback += 1
            elif attribution == "none":
                n_unparseable += 1

            for col in reads:
                self._touch(acc, col, ts, e, is_write=False, attribution=attribution)
            for col in writes:
                self._touch(acc, col, ts, e, is_write=True, attribution=attribution)

        return self._score(acc, now, cutoff, len(entries), n_in_window,
                           n_parsed, n_fallback, n_unparseable)

    # ------------------------------------------------------------------ #

    def _columns_for(self, e: LogEntry) -> tuple[set[str], set[str], str]:
        parsed: Optional[QueryColumns] = None
        if self.mode in ("sql", "hybrid") and e.sql:
            parsed = self.parser.parse(e.sql)          # cached; do not mutate
            if parsed.wildcards and self.metadata:
                parsed = self._expand_wildcards(parsed)

        declared_r, declared_w = set(e.declared_reads), set(e.declared_writes)

        if self.mode == "declared":
            return declared_r, declared_w, ("declared" if e.has_declared_columns else "none")

        if self.mode == "sql":
            if parsed and parsed.parsed_ok:
                return set(parsed.reads), set(parsed.writes), "sqlglot"
            if e.has_declared_columns:
                return declared_r, declared_w, "declared"
            return set(), set(), "none"

        # hybrid: union of a good parse and the declared lists
        if parsed and parsed.parsed_ok:
            return parsed.reads | declared_r, parsed.writes | declared_w, "sqlglot"
        if e.has_declared_columns:
            return declared_r, declared_w, "declared"
        return set(), set(), "none"

    def _expand_wildcards(self, qc: QueryColumns) -> QueryColumns:
        """Return a copy with `table.*` wildcards expanded from metadata."""
        reads = set(qc.reads)
        leftover = set()
        for w in qc.wildcards:
            table = w.rsplit(".", 1)[0]
            t = self.metadata.table(table)
            if t:
                reads |= {f"{table}.{c.name}" for c in t.columns}
            else:
                leftover.add(w)
        return QueryColumns(reads=reads, writes=set(qc.writes), wildcards=leftover,
                            unresolved=list(qc.unresolved), parsed_ok=qc.parsed_ok,
                            method=qc.method)

    def _touch(self, acc, col, ts, e: LogEntry, *, is_write: bool, attribution: str):
        a = acc.get(col)
        if a is None:
            a = acc[col] = _Acc()
        if is_write:
            a.writes += 1
            a.last_write = ts if a.last_write is None or ts > a.last_write else a.last_write
        else:
            a.reads += 1
            a.last_read = ts if a.last_read is None or ts > a.last_read else a.last_read
        a.first = ts if a.first is None or ts < a.first else a.first
        a.days.add(ts.date())
        if e.service:
            a.services[e.service] = a.services.get(e.service, 0) + 1
        if e.template:
            a.templates.add(e.template)
        if attribution == "sqlglot":
            a.from_sql += 1
        elif attribution == "declared":
            a.from_declared += 1

    # ------------------------------------------------------------------ #

    def _score(self, acc, now, cutoff, n_total, n_in_window,
               n_parsed, n_fallback, n_unparseable) -> UsageResult:
        # every metadata column gets a row; unseen ones score 0
        all_cols: list[str]
        if self.metadata:
            all_cols = [c.qualified_name for c in self.metadata.iter_columns()]
            for seen in acc:
                if seen not in all_cols:
                    all_cols.append(seen)
        else:
            all_cols = list(acc)

        weighted = {
            col: acc[col].reads + WRITE_WEIGHT * acc[col].writes
            for col in acc
        }
        # percentile rank is taken over EVERY column (0-usage ones included),
        # so the score spreads across the whole schema
        rank_basis = sorted(weighted.get(col, 0.0) for col in all_cols)
        log_abs_ref = math.log1p(ABS_REFERENCE)

        out: list[ColumnUsage] = []
        for col in all_cols:
            a = acc.get(col)
            table, _, column = col.rpartition(".")
            if a is None or (a.reads == 0 and a.writes == 0):
                out.append(ColumnUsage(
                    qualified_name=col, table=table, column=column,
                    score=0.0, is_used=False, tier="unused",
                    read_count=0, write_count=0, total_count=0,
                    last_read_at=None, last_write_at=None, last_access_at=None,
                    first_access_at=None, distinct_days=0, distinct_services=0,
                    access_by_type={"read": 0, "write": 0}, access_by_service={},
                    query_templates=[], window_days=self.window_days,
                    attribution="none",
                    signals={"reason": "no query in window touched this column"},
                ))
                continue

            w = weighted[col]
            rank = _rank_fraction(rank_basis, w)
            absolute = min(1.0, math.log1p(w) / log_abs_ref)
            freq = RANK_W * rank + (1 - RANK_W) * absolute
            last_access = max(t for t in (a.last_read, a.last_write) if t is not None)
            age_days = max(0.0, (now - last_access).total_seconds() / 86400)
            recency = 0.5 ** (age_days / self.half_life)
            score = freq * (RECENCY_FLOOR + (1 - RECENCY_FLOOR) * recency)
            score = round(min(1.0, max(0.0, score)), 4)

            total = a.reads + a.writes
            out.append(ColumnUsage(
                qualified_name=col, table=table, column=column,
                score=score, is_used=True, tier=_tier(score),
                read_count=a.reads, write_count=a.writes, total_count=total,
                last_read_at=a.last_read.isoformat() if a.last_read else None,
                last_write_at=a.last_write.isoformat() if a.last_write else None,
                last_access_at=last_access.isoformat(),
                first_access_at=a.first.isoformat() if a.first else None,
                distinct_days=len(a.days),
                distinct_services=len(a.services),
                access_by_type={"read": a.reads, "write": a.writes},
                access_by_service=dict(sorted(a.services.items(), key=lambda kv: -kv[1])),
                query_templates=sorted(a.templates),
                window_days=self.window_days,
                attribution=("sqlglot" if a.from_sql and not a.from_declared
                             else "declared" if a.from_declared and not a.from_sql
                             else "hybrid" if a.from_sql and a.from_declared
                             else "none"),
                signals={
                    "weighted_accesses": round(w, 2),
                    "rank_fraction": round(rank, 4),
                    "absolute_component": round(absolute, 4),
                    "frequency_component": round(freq, 4),
                    "recency_component": round(recency, 4),
                    "age_days": round(age_days, 1),
                },
            ))

        out.sort(key=lambda c: (-c.score, c.qualified_name))
        return UsageResult(
            columns=out, window_days=self.window_days,
            window_start=cutoff.isoformat(), window_end=now.isoformat(),
            n_queries=n_total, n_in_window=n_in_window,
            n_parsed_sql=n_parsed, n_fallback_declared=n_fallback,
            n_unparseable=n_unparseable, mode=self.mode,
        )


def _rank_fraction(sorted_values: list[float], value: float) -> float:
    """Fraction of columns this value ranks at or above (mid-rank for ties)."""
    n = len(sorted_values)
    if n <= 1:
        return 1.0 if value > 0 else 0.0
    import bisect

    lo = bisect.bisect_left(sorted_values, value)
    hi = bisect.bisect_right(sorted_values, value)
    mid = (lo + hi) / 2
    return mid / (n - 1)


def analyze_log(
    log: str | Path | Iterable[LogEntry],
    metadata: Optional[DatabaseMetadata] = None,
    **kwargs,
) -> UsageResult:
    return UsageAnalyzer(metadata, **kwargs).analyze(log)
