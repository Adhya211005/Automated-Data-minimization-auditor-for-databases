"""Schema + sample-value extraction.

Produces one ``DatabaseMetadata`` object describing every table and column in
the target schema. This is the *only* contract between the ingestion layer and
the analysis engines - Phases 2-4 each take a ``DatabaseMetadata`` (live, or
reloaded from the JSON this module writes) and never touch the database
directly.

Shape (per column) and who uses it:

    qualified_name        all           "table.column", the join key everywhere
    table / name / ordinal all
    generic_type          sensitivity, retention   normalized type family
    raw_type              sensitivity   DB-native type string
    nullable / default    sensitivity, retention   default 'now()' hints "created" ts
    comment               sensitivity   free-text hint
    is_primary_key        usage         keys are structurally "used"
    is_foreign_key        usage, sensitivity
    foreign_key_target    usage
    is_unique             sensitivity   unique + string often => identifier
    char_max_length       sensitivity
    is_temporal           retention     candidate for age checks
    sample_values         sensitivity   the main classification signal
    sample_null_fraction  sensitivity
    distinct_in_sample    sensitivity
    min_value / max_value retention     span of a temporal column (pre-fetched)

Per table: row_count, primary_key, foreign_keys, temporal_columns.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from auditor import __version__
from auditor.ingestion.connector import ReadOnlyConnector

# --------------------------------------------------------------------------- #
# Type normalization
# --------------------------------------------------------------------------- #

_TEMPORAL = {"date", "timestamp", "timestamptz", "time", "timetz"}


def _generic_type(raw_type: str) -> str:
    """Collapse a DB-native type string into a small, stable family name."""
    t = raw_type.lower().split("(")[0].strip()
    table = {
        "character varying": "string", "varchar": "string", "character": "string",
        "char": "string", "bpchar": "string", "name": "string", "citext": "string",
        "text": "text",
        "smallint": "integer", "integer": "integer", "int": "integer", "int2": "integer",
        "int4": "integer", "int8": "integer", "bigint": "integer", "serial": "integer",
        "bigserial": "integer",
        "numeric": "numeric", "decimal": "numeric", "money": "numeric",
        "real": "float", "double precision": "float", "float": "float", "float4": "float",
        "float8": "float",
        "boolean": "boolean", "bool": "boolean",
        "date": "date",
        "timestamp without time zone": "timestamp", "timestamp": "timestamp",
        "timestamp with time zone": "timestamptz", "timestamptz": "timestamptz",
        "time without time zone": "time", "time": "time",
        "time with time zone": "timetz", "timetz": "timetz",
        "interval": "interval",
        "uuid": "uuid",
        "inet": "inet", "cidr": "inet", "macaddr": "inet", "macaddr8": "inet",
        "json": "json", "jsonb": "json",
        "bytea": "bytes", "blob": "bytes",
        "ARRAY": "array",
    }
    if t in table:
        return table[t]
    if t.endswith("[]") or t == "array":
        return "array"
    if "timestamp" in t:
        return "timestamptz" if "with time zone" in t else "timestamp"
    if "char" in t or "text" in t:
        return "string"
    if "int" in t:
        return "integer"
    return "other"


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of a sampled value to something JSON-safe.
    Sample values exist mainly for regex-style inspection by the classifier,
    so everything non-native is rendered to a string."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple, dict)):
        try:
            return json.dumps(value, default=str)
        except TypeError:
            return str(value)
    return str(value)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class ColumnMetadata:
    table: str
    name: str
    ordinal: int
    generic_type: str
    raw_type: str
    nullable: bool
    default: Optional[str] = None
    comment: Optional[str] = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_target: Optional[str] = None
    is_unique: bool = False
    is_indexed: bool = False
    char_max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    is_temporal: bool = False
    sample_values: list[Any] = field(default_factory=list)
    sample_size: int = 0
    sample_null_fraction: Optional[float] = None
    distinct_in_sample: Optional[int] = None
    min_value: Optional[str] = None
    max_value: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.name}"

    def to_dict(self) -> dict:
        # qualified_name is derived, but emitting it keeps JSON consumers
        # (Phases 2-4 reading a saved dump) from having to recompute the join key.
        return {"qualified_name": self.qualified_name, **dataclasses.asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "ColumnMetadata":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TableMetadata:
    name: str
    schema: str
    row_count: int
    row_count_exact: bool = True
    comment: Optional[str] = None
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)
    columns: list[ColumnMetadata] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def temporal_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_temporal]

    def column(self, name: str) -> Optional[ColumnMetadata]:
        return next((c for c in self.columns if c.name == name), None)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "schema": self.schema,
            "row_count": self.row_count,
            "row_count_exact": self.row_count_exact,
            "comment": self.comment,
            "primary_key": list(self.primary_key),
            "foreign_keys": [dict(fk) for fk in self.foreign_keys],
            "temporal_columns": self.temporal_columns,
            "columns": [c.to_dict() for c in self.columns],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TableMetadata":
        return cls(
            name=d["name"],
            schema=d["schema"],
            row_count=d["row_count"],
            row_count_exact=d.get("row_count_exact", True),
            comment=d.get("comment"),
            primary_key=list(d.get("primary_key", [])),
            foreign_keys=[dict(fk) for fk in d.get("foreign_keys", [])],
            columns=[ColumnMetadata.from_dict(c) for c in d.get("columns", [])],
        )


@dataclass
class DatabaseMetadata:
    dialect: str
    database: str
    schema: str
    generated_at: str
    auditor_version: str
    tables: list[TableMetadata] = field(default_factory=list)

    # -- lookups -----------------------------------------------------------
    def table(self, name: str) -> Optional[TableMetadata]:
        return next((t for t in self.tables if t.name == name), None)

    def column(self, qualified_name: str) -> Optional[ColumnMetadata]:
        if "." not in qualified_name:
            return None
        tbl, _, col = qualified_name.rpartition(".")
        t = self.table(tbl)
        return t.column(col) if t else None

    def iter_columns(self) -> Iterator[ColumnMetadata]:
        for t in self.tables:
            yield from t.columns

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    @property
    def column_names(self) -> list[str]:
        return [c.qualified_name for c in self.iter_columns()]

    # -- serialization ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "database": self.database,
            "schema": self.schema,
            "generated_at": self.generated_at,
            "auditor_version": self.auditor_version,
            "table_count": len(self.tables),
            "column_count": sum(len(t.columns) for t in self.tables),
            "tables": [t.to_dict() for t in self.tables],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=_jsonable)

    @classmethod
    def from_dict(cls, d: dict) -> "DatabaseMetadata":
        return cls(
            dialect=d["dialect"],
            database=d["database"],
            schema=d["schema"],
            generated_at=d["generated_at"],
            auditor_version=d.get("auditor_version", "unknown"),
            tables=[TableMetadata.from_dict(t) for t in d.get("tables", [])],
        )

    @classmethod
    def from_json(cls, s: str) -> "DatabaseMetadata":
        return cls.from_dict(json.loads(s))

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "DatabaseMetadata":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def summary_table(self) -> str:
        rows = [
            f"{self.dialect}://{self.database}  schema={self.schema}  "
            f"({len(self.tables)} tables, {sum(len(t.columns) for t in self.tables)} columns)"
        ]
        for t in self.tables:
            rows.append(f"\n  {t.name}  ({t.row_count:,} rows)")
            for c in t.columns:
                tags = []
                if c.is_primary_key:
                    tags.append("pk")
                if c.is_foreign_key:
                    tags.append(f"fk->{c.foreign_key_target}")
                if c.is_unique:
                    tags.append("uniq")
                if c.is_temporal:
                    tags.append("temporal")
                tag_s = f"  [{', '.join(tags)}]" if tags else ""
                sample = ", ".join(str(v) for v in c.sample_values[:3])
                if len(sample) > 58:
                    sample = sample[:55] + "..."
                rows.append(
                    f"      {c.name:<22} {c.generic_type:<11} "
                    f"{'NULL' if c.nullable else 'NOT NULL':<8}{tag_s}"
                    + (f"\n          e.g. {sample}" if sample else "")
                )
        return "\n".join(rows)


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #


class MetadataExtractor:
    """Pulls a ``DatabaseMetadata`` out of a target database over a verified
    read-only connection."""

    def __init__(self, connector: ReadOnlyConnector):
        self.connector = connector
        self.config = connector.config

    def extract(
        self,
        *,
        tables: Optional[list[str]] = None,
        sample_rows: int = 30,
        max_samples: int = 12,
    ) -> DatabaseMetadata:
        schema = self.config.schema
        with self.connector.connect() as conn:
            insp = inspect(conn)
            all_tables = insp.get_table_names(schema=schema)
            wanted = [t for t in all_tables if (tables is None or t in tables)]
            if tables:
                missing = sorted(set(tables) - set(all_tables))
                if missing:
                    raise ValueError(f"tables not found in schema {schema!r}: {missing}")

            table_meta = [
                self._extract_table(conn, insp, schema, name, sample_rows, max_samples)
                for name in sorted(wanted)
            ]

        return DatabaseMetadata(
            dialect=self.config.dialect,
            database=self.config.dbname,
            schema=schema,
            generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            auditor_version=__version__,
            tables=table_meta,
        )

    # ------------------------------------------------------------------ #

    def _extract_table(self, conn, insp, schema, name, sample_rows, max_samples) -> TableMetadata:
        cols_raw = insp.get_columns(name, schema=schema)
        pk = insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or []
        fks = insp.get_foreign_keys(name, schema=schema)
        uniques = insp.get_unique_constraints(name, schema=schema)
        indexes = insp.get_indexes(name, schema=schema)
        try:
            tbl_comment = (insp.get_table_comment(name, schema=schema) or {}).get("text")
        except NotImplementedError:  # pragma: no cover
            tbl_comment = None

        unique_cols = {c for u in uniques for c in u.get("column_names", [])}
        unique_cols |= {c for c in pk}  # pk implies unique
        indexed_cols = {c for ix in indexes for c in ix.get("column_names", []) if c}
        fk_target: dict[str, str] = {}
        for fk in fks:
            for local, remote in zip(fk["constrained_columns"], fk["referred_columns"]):
                rt = fk.get("referred_table")
                fk_target[local] = f"{rt}.{remote}"

        columns: list[ColumnMetadata] = []
        for i, c in enumerate(cols_raw):
            raw_type = str(c["type"])
            gtype = _generic_type(raw_type)
            # SQLAlchemy renders both timestamp and timestamptz as "TIMESTAMP";
            # the tz distinction lives on the type object and matters to the
            # retention checker, so recover it here.
            if gtype in ("timestamp", "timestamptz") and getattr(c["type"], "timezone", False):
                gtype = "timestamptz"
            elif gtype in ("time", "timetz") and getattr(c["type"], "timezone", False):
                gtype = "timetz"
            default = c.get("default")
            columns.append(
                ColumnMetadata(
                    table=name,
                    name=c["name"],
                    ordinal=i,
                    generic_type=gtype,
                    raw_type=raw_type,
                    nullable=bool(c.get("nullable", True)),
                    default=str(default) if default is not None else None,
                    comment=c.get("comment"),
                    is_primary_key=c["name"] in pk,
                    is_foreign_key=c["name"] in fk_target,
                    foreign_key_target=fk_target.get(c["name"]),
                    is_unique=c["name"] in unique_cols,
                    is_indexed=c["name"] in indexed_cols,
                    char_max_length=getattr(c["type"], "length", None),
                    numeric_precision=getattr(c["type"], "precision", None),
                    is_temporal=gtype in _TEMPORAL,
                )
            )

        row_count = self._count_rows(conn, schema, name)
        if row_count:
            self._add_samples(conn, schema, name, columns, sample_rows, max_samples)
            self._add_temporal_spans(conn, schema, name, columns)

        return TableMetadata(
            name=name,
            schema=schema,
            row_count=row_count,
            row_count_exact=True,
            comment=tbl_comment,
            primary_key=list(pk),
            foreign_keys=[
                {
                    "columns": list(fk["constrained_columns"]),
                    "ref_table": fk.get("referred_table"),
                    "ref_columns": list(fk["referred_columns"]),
                }
                for fk in fks
            ],
            columns=columns,
        )

    # ------------------------------------------------------------------ #

    def _q(self, conn: Connection, schema: str, table: str) -> str:
        prep = conn.dialect.identifier_preparer
        return f"{prep.quote_schema(schema)}.{prep.quote(table)}"

    def _count_rows(self, conn, schema, table) -> int:
        return int(conn.execute(text(f"SELECT count(*) FROM {self._q(conn, schema, table)}")).scalar_one())

    def _add_samples(self, conn, schema, table, columns, sample_rows, max_samples) -> None:
        prep = conn.dialect.identifier_preparer
        col_list = ", ".join(prep.quote(c.name) for c in columns)
        rand = "random()" if self.config.dialect == "postgresql" else "rand()"
        sql = f"SELECT {col_list} FROM {self._q(conn, schema, table)} ORDER BY {rand} LIMIT :n"
        rows = conn.execute(text(sql), {"n": sample_rows}).fetchall()
        n = len(rows)
        for idx, col in enumerate(columns):
            raw_vals = [r[idx] for r in rows]
            non_null = [v for v in raw_vals if v is not None]
            col.sample_size = n
            col.sample_null_fraction = round((n - len(non_null)) / n, 4) if n else None
            seen: list[Any] = []
            for v in non_null:
                jv = _jsonable(v)
                if jv not in seen:
                    seen.append(jv)
                if len(seen) >= max_samples:
                    break
            col.sample_values = seen
            col.distinct_in_sample = len({_jsonable(v) for v in non_null}) if non_null else 0

    def _add_temporal_spans(self, conn, schema, table, columns) -> None:
        temporal = [c for c in columns if c.is_temporal]
        if not temporal:
            return
        prep = conn.dialect.identifier_preparer
        parts = []
        for c in temporal:
            parts.append(f"min({prep.quote(c.name)})")
            parts.append(f"max({prep.quote(c.name)})")
        sql = f"SELECT {', '.join(parts)} FROM {self._q(conn, schema, table)}"
        row = conn.execute(text(sql)).fetchone()
        for j, c in enumerate(temporal):
            lo, hi = row[2 * j], row[2 * j + 1]
            c.min_value = _jsonable(lo) if lo is not None else None
            c.max_value = _jsonable(hi) if hi is not None else None


def extract_metadata(
    connector: Optional[ReadOnlyConnector] = None,
    *,
    tables: Optional[list[str]] = None,
    sample_rows: int = 30,
    max_samples: int = 12,
    **connector_kwargs,
) -> DatabaseMetadata:
    """Convenience wrapper: extract from an existing connector, or build one
    from the environment."""
    owned = connector is None
    conn = connector or ReadOnlyConnector.from_env(**connector_kwargs)
    try:
        return MetadataExtractor(conn).extract(
            tables=tables, sample_rows=sample_rows, max_samples=max_samples
        )
    finally:
        if owned:
            conn.dispose()
