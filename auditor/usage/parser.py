"""Attribute a SQL statement to the columns it touches.

Built on ``sqlglot``. Handles:

  * SELECT   - projection, WHERE, JOIN ... ON, GROUP BY, HAVING, ORDER BY,
               scalar/derived subqueries and CTEs (columns inside them count
               as reads), ``SELECT *`` expansion when a schema is supplied
  * INSERT   - the explicit ``(col, col, ...)`` list -> writes;
               ``INSERT ... SELECT`` -> the SELECT's columns are reads
  * UPDATE   - ``SET col = ...`` targets -> writes; the assigned expressions
               and the WHERE / FROM clause -> reads
  * DELETE   - WHERE columns -> reads

Table aliases are resolved from the FROM/JOIN clause. Unqualified columns are
resolved against the supplied schema when the table is unambiguous.

Known limitations (see auditor/usage/README.md):
  * ``SELECT *`` with no schema -> recorded as a ``table.*`` wildcard, not
    expanded.
  * Columns owned by a CTE / derived table whose own SELECT can't be traced
    back to base columns are reported under the CTE name in ``unresolved``.
  * Dynamic SQL, ``EXECUTE``, and statements sqlglot cannot parse fall back
    to the log's declared column lists (the analyzer handles that).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from auditor.ingestion.metadata import DatabaseMetadata


@dataclass
class QueryColumns:
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    wildcards: set[str] = field(default_factory=set)      # "table.*" - SELECT * not expandable
    unresolved: list[str] = field(default_factory=list)   # columns we could not tie to a base table
    parsed_ok: bool = False
    method: str = "none"                                  # "sqlglot" | "insert-regex" | "none"

    @property
    def all_columns(self) -> set[str]:
        return self.reads | self.writes


class QueryParser:
    def __init__(
        self,
        metadata: Optional[DatabaseMetadata] = None,
        *,
        dialect: str = "postgres",
    ):
        self.dialect = dialect
        self._table_columns: dict[str, set[str]] = {}
        if metadata is not None:
            for t in metadata.tables:
                self._table_columns[t.name] = {c.name for c in t.columns}
        self._schema = (
            {t: {c: "UNKNOWN" for c in cols} for t, cols in self._table_columns.items()}
            or None
        )
        self._cache: dict[str, QueryColumns] = {}

    @classmethod
    def from_schema(cls, schema: dict[str, list[str]], *, dialect: str = "postgres") -> "QueryParser":
        """Build a parser from a plain ``{table: [columns]}`` map (tests / no metadata)."""
        p = cls.__new__(cls)
        p.dialect = dialect
        p._table_columns = {t: set(cols) for t, cols in schema.items()}
        p._schema = {t: {c: "UNKNOWN" for c in cols} for t, cols in p._table_columns.items()} or None
        p._cache = {}
        return p

    # ------------------------------------------------------------------ #

    def parse(self, sql: str) -> QueryColumns:
        sql = (sql or "").strip()
        if not sql:
            return QueryColumns()
        cached = self._cache.get(sql)
        if cached is not None:
            return cached
        result = self._parse_uncached(sql)
        self._cache[sql] = result
        return result

    def _parse_uncached(self, sql: str) -> QueryColumns:
        try:
            root = sqlglot.parse_one(sql, read=self.dialect)
        except (ParseError, Exception):  # noqa: BLE001 - sqlglot raises various types
            return self._insert_regex_fallback(sql)

        if root is None:
            return self._insert_regex_fallback(sql)

        try:
            if isinstance(root, exp.Insert):
                return self._parse_insert(root, sql)
            if isinstance(root, exp.Update):
                return self._parse_update(root)
            if isinstance(root, exp.Delete):
                return self._parse_delete(root)
            return self._parse_select(root)
        except Exception:  # noqa: BLE001 - never let a parser bug crash analysis
            return self._insert_regex_fallback(sql)

    # ------------------------------------------------------------------ #

    def _alias_map(self, node: exp.Expression) -> tuple[dict[str, str], set[str]]:
        """alias/name -> real table name, plus the set of real tables referenced."""
        amap: dict[str, str] = {}
        tables: set[str] = set()
        for tbl in node.find_all(exp.Table):
            real = tbl.name
            tables.add(real)
            amap[real] = real
            alias = tbl.alias
            if alias:
                amap[alias] = real
        return amap, tables

    def _resolver(self, amap: dict[str, str], tables: set[str], sink: QueryColumns):
        single = next(iter(tables)) if len(tables) == 1 else None
        known = self._table_columns

        def resolve(col: exp.Column) -> Optional[str]:
            name = col.name
            if not name or name == "*":
                return None
            raw_tbl = col.table
            if raw_tbl:
                tbl = amap.get(raw_tbl, raw_tbl)
            elif single:
                tbl = single
            else:
                # unqualified in a multi-table query: use the schema if unambiguous
                owners = [t for t in tables if name in known.get(t, ())]
                tbl = owners[0] if len(owners) == 1 else None
            if tbl is None:
                sink.unresolved.append(name)
                return None
            if known and tbl not in known:
                sink.unresolved.append(f"{tbl}.{name}")
                return None
            if known and name not in known[tbl]:
                # column not in that table's schema (CTE-projected alias, etc.)
                sink.unresolved.append(f"{tbl}.{name}")
                return None
            return f"{tbl}.{name}"

        return resolve

    # ------------------------------------------------------------------ #

    def _parse_select(self, root: exp.Expression) -> QueryColumns:
        out = QueryColumns(parsed_ok=True, method="sqlglot")

        expanded = root
        if self._schema:
            try:
                expanded = qualify(
                    root.copy(), schema=self._schema, dialect=self.dialect,
                    validate_qualify_columns=False, quote_identifiers=False, identify=False,
                    expand_stars=True,
                )
            except (OptimizeError, Exception):  # noqa: BLE001
                expanded = root

        amap, tables = self._alias_map(expanded)
        resolve = self._resolver(amap, tables, out)

        for col in expanded.find_all(exp.Column):
            r = resolve(col)
            if r:
                out.reads.add(r)

        # unexpanded SELECT * / t.* (no schema): record wildcards.
        # A Star inside a function - count(*) - is not a column wildcard.
        for star in expanded.find_all(exp.Star):
            parent = star.parent
            if isinstance(parent, exp.Column):            # t.*
                tname = amap.get(parent.table, parent.table) if parent.table else None
                if tname:
                    out.wildcards.add(f"{tname}.*")
                elif len(tables) == 1:
                    out.wildcards.add(f"{next(iter(tables))}.*")
            elif isinstance(parent, (exp.Select, exp.Pivot)):   # bare SELECT *
                if len(tables) == 1:
                    out.wildcards.add(f"{next(iter(tables))}.*")
                else:
                    out.wildcards.update(f"{t}.*" for t in tables)
            # else: star is a function arg (count(*), etc.) - ignore

        out.parsed_ok = bool(out.reads or out.wildcards)
        return out

    def _parse_insert(self, root: exp.Insert, sql: str) -> QueryColumns:
        out = QueryColumns(parsed_ok=True, method="sqlglot")
        schema_node = root.this  # exp.Schema (Table + column ids) or exp.Table
        table = None
        cols: list[str] = []
        if isinstance(schema_node, exp.Schema):
            tbl = schema_node.this
            table = tbl.name if isinstance(tbl, exp.Table) else None
            cols = [c.name for c in schema_node.expressions if isinstance(c, exp.Identifier | exp.Column)]
        elif isinstance(schema_node, exp.Table):
            table = schema_node.name

        if table and cols:
            for c in cols:
                out.writes.add(f"{table}.{c}")
        elif table:
            out.wildcards.add(f"{table}.*")  # INSERT with no column list

        # INSERT ... SELECT: the source columns are reads
        src = root.expression
        if isinstance(src, exp.Expression) and src.find(exp.Column):
            sub = self._parse_select(src)
            out.reads |= sub.reads
            out.unresolved += sub.unresolved

        if not (out.writes or out.wildcards):
            return self._insert_regex_fallback(sql)
        out.parsed_ok = True
        return out

    def _parse_update(self, root: exp.Update) -> QueryColumns:
        out = QueryColumns(parsed_ok=True, method="sqlglot")
        target = root.this
        amap, tables = self._alias_map(root)
        if isinstance(target, exp.Table):
            tables.add(target.name)
            amap.setdefault(target.name, target.name)
            if target.alias:
                amap[target.alias] = target.name
        resolve = self._resolver(amap, tables, out)

        for setexpr in root.args.get("expressions", []) or []:
            if isinstance(setexpr, exp.EQ) and isinstance(setexpr.this, exp.Column):
                w = resolve(setexpr.this)
                if w:
                    out.writes.add(w)
                for c in setexpr.expression.find_all(exp.Column):
                    r = resolve(c)
                    if r:
                        out.reads.add(r)
            else:
                for c in setexpr.find_all(exp.Column):
                    r = resolve(c)
                    if r:
                        out.reads.add(r)

        for clause in ("where", "from", "with"):
            node = root.args.get(clause)
            if node:
                for c in node.find_all(exp.Column):
                    r = resolve(c)
                    if r:
                        out.reads.add(r)

        out.parsed_ok = bool(out.writes or out.reads)
        return out

    def _parse_delete(self, root: exp.Delete) -> QueryColumns:
        out = QueryColumns(parsed_ok=True, method="sqlglot")
        amap, tables = self._alias_map(root)
        resolve = self._resolver(amap, tables, out)
        where = root.args.get("where")
        if where:
            for c in where.find_all(exp.Column):
                r = resolve(c)
                if r:
                    out.reads.add(r)
        out.parsed_ok = True
        return out

    # ------------------------------------------------------------------ #

    _INSERT_RX = None

    def _insert_regex_fallback(self, sql: str) -> QueryColumns:
        import re

        if QueryParser._INSERT_RX is None:
            QueryParser._INSERT_RX = re.compile(
                r"insert\s+into\s+[\"'`]?([A-Za-z_][\w$]*)[\"'`]?\s*\(([^)]+)\)",
                re.I | re.S,
            )
        m = QueryParser._INSERT_RX.search(sql or "")
        if not m:
            return QueryColumns(parsed_ok=False, method="none")
        table, collist = m.group(1), m.group(2)
        out = QueryColumns(parsed_ok=True, method="insert-regex")
        for raw in collist.split(","):
            name = raw.strip().strip('"').strip("'").strip("`")
            if name:
                out.writes.add(f"{table}.{name}")
        return out
