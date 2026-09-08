"""Read a query log into ``LogEntry`` records.

Supports the Phase 0 formats: newline-delimited JSON (``.jsonl``) and CSV
(``.csv`` with ``|``-joined column lists). A real deployment would add a
``pg_stat_statements`` / PostgreSQL CSV-log reader here - the rest of the
pipeline only depends on ``LogEntry``.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator


@dataclass
class LogEntry:
    ts: datetime
    query_id: str
    operation: str                     # SELECT | INSERT | UPDATE | DELETE | ...
    sql: str = ""
    service: str = ""
    db_user: str = ""
    template: str = ""
    declared_reads: list[str] = field(default_factory=list)    # columns_read from the log, if present
    declared_writes: list[str] = field(default_factory=list)   # columns_written from the log, if present
    rows: int | None = None

    @property
    def has_declared_columns(self) -> bool:
        return bool(self.declared_reads or self.declared_writes)


def _parse_ts(raw: str) -> datetime:
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _split_cols(raw: str) -> list[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.replace(";", "|").split("|") if c.strip()]


def read_query_log(path: str | Path) -> Iterator[LogEntry]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson", ".json"):
        yield from _read_jsonl(path)
    elif suffix == ".csv":
        yield from _read_csv(path)
    else:
        # sniff: JSON object per line?
        first = path.read_text(encoding="utf-8-sig").lstrip()[:1]
        if first == "{":
            yield from _read_jsonl(path)
        else:
            yield from _read_csv(path)


def _read_jsonl(path: Path) -> Iterator[LogEntry]:
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            yield LogEntry(
                ts=_parse_ts(e["ts"]),
                query_id=str(e.get("query_id", "")),
                operation=str(e.get("operation", "")).upper(),
                sql=e.get("sql", "") or "",
                service=e.get("service", "") or "",
                db_user=e.get("db_user", "") or "",
                template=e.get("template", "") or "",
                declared_reads=list(e.get("columns_read", []) or []),
                declared_writes=list(e.get("columns_written", []) or []),
                rows=e.get("rows"),
            )


def _read_csv(path: Path) -> Iterator[LogEntry]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            yield LogEntry(
                ts=_parse_ts(row["ts"]),
                query_id=row.get("query_id", ""),
                operation=row.get("operation", "").upper(),
                sql=row.get("sql", "") or "",
                service=row.get("service", "") or "",
                db_user=row.get("db_user", "") or "",
                template=row.get("template", "") or "",
                declared_reads=_split_cols(row.get("columns_read", "")),
                declared_writes=_split_cols(row.get("columns_written", "")),
                rows=int(row["rows"]) if row.get("rows") else None,
            )
