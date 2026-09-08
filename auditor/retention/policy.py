"""Retention policy - a simple day-threshold config.

Resolution order for a column: column override -> table override -> default.

File format (YAML or JSON), everything except ``default_days`` optional::

    default_days: 365
    tables:
      orders: 2555            # keep order history 7 years (tax)
      support_tickets: 365
    columns:
      users.last_login_at: 730
    anchors:
      users: created_at        # which timestamp marks a row's age (default: created_at)
    exclude:
      - "*.updated_at"         # never flag these (glob on table.column)
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RetentionPolicy:
    default_days: int = 365
    tables: dict[str, int] = field(default_factory=dict)
    columns: dict[str, int] = field(default_factory=dict)      # "table.column" -> days
    anchors: dict[str, str] = field(default_factory=dict)      # table -> anchor column
    exclude: list[str] = field(default_factory=list)           # globs on "table.column"

    # -- resolution ------------------------------------------------------
    def days_for(self, table: str, column: str | None = None) -> tuple[int, str]:
        if column is not None:
            qn = f"{table}.{column}"
            if qn in self.columns:
                return self.columns[qn], "column"
        if table in self.tables:
            return self.tables[table], "table"
        return self.default_days, "default"

    def anchor_for(self, table: str) -> str | None:
        return self.anchors.get(table)

    def is_excluded(self, table: str, column: str) -> bool:
        qn = f"{table}.{column}"
        return any(fnmatch.fnmatch(qn, pat) for pat in self.exclude)

    # -- io ------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "RetentionPolicy":
        return cls(
            default_days=int(d.get("default_days", 365)),
            tables={k: int(v) for k, v in (d.get("tables") or {}).items()},
            columns={k: int(v) for k, v in (d.get("columns") or {}).items()},
            anchors=dict(d.get("anchors") or {}),
            exclude=list(d.get("exclude") or []),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "RetentionPolicy":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        return {
            "default_days": self.default_days,
            "tables": dict(self.tables),
            "columns": dict(self.columns),
            "anchors": dict(self.anchors),
            "exclude": list(self.exclude),
        }
