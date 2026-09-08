"""Draft a SQL remediation for a flagged column.

Strategy:
    * column is a primary/foreign key      -> anonymize_in_place (can't safely drop)
    * otherwise                            -> archive_then_drop

Every script is wrapped in comments carrying the necessity breakdown and the
reasons, plus cautions (legal retention, backup, row-level alternative). It is
a draft: nothing connects to a database, nothing runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

ARCHIVE_SCHEMA = "dma_archive"

_HIGH_RETENTION_PII = {"government_id", "financial", "health", "credential"}


# --------------------------------------------------------------------------- #
# context assembled from either a live NecessityScore or a stored finding
# --------------------------------------------------------------------------- #

@dataclass
class _Ctx:
    schema: str
    table: str
    column: str
    dialect: str = "postgresql"

    verdict: str = ""
    necessity: float = 0.0
    sensitivity: float = 0.0
    usage: float = 0.0
    retention: float = 0.0
    thresholds: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    pii_type: Optional[str] = None
    tier: Optional[str] = None
    matched_rules: list[str] = field(default_factory=list)

    read_count: Optional[int] = None
    write_count: Optional[int] = None
    last_access_at: Optional[str] = None
    window_days: Optional[int] = None

    days_overdue: Optional[int] = None
    policy_days: Optional[int] = None
    oldest_row_age_days: Optional[int] = None
    fraction_overdue: Optional[float] = None
    retention_basis: Optional[str] = None
    anchor_column: Optional[str] = None

    primary_key: list[str] = field(default_factory=list)
    raw_type: str = "text"
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_target: Optional[str] = None

    @classmethod
    def from_score(cls, ns, col_meta, table_meta, *, dialect="postgresql", schema="public") -> "_Ctx":
        ev = ns.evidence or {}
        s_ev, u_ev, r_ev = ev.get("sensitivity", {}), ev.get("usage", {}), ev.get("retention", {})
        pol = r_ev.get("policy_applied") or {}
        b = ns.breakdown or {}
        return cls(
            schema=(table_meta.schema if table_meta else schema),
            table=ns.table, column=ns.column, dialect=dialect,
            verdict=ns.verdict, necessity=ns.score,
            sensitivity=ns.sensitivity, usage=ns.usage, retention=ns.retention,
            thresholds={
                "sensitive": b.get("sensitivity", {}).get("threshold"),
                "unused": b.get("usage", {}).get("threshold"),
                "stale": b.get("staleness", {}).get("threshold"),
            },
            reasons=list(ns.reasons or []),
            pii_type=s_ev.get("pii_type"), tier=s_ev.get("tier"),
            matched_rules=list(s_ev.get("matched_rules") or []),
            read_count=u_ev.get("read_count"), write_count=u_ev.get("write_count"),
            last_access_at=u_ev.get("last_access_at"), window_days=u_ev.get("window_days"),
            days_overdue=r_ev.get("days_overdue"),
            policy_days=pol.get("days"), anchor_column=pol.get("anchor"),
            oldest_row_age_days=r_ev.get("oldest_row_age_days"),
            fraction_overdue=r_ev.get("fraction_overdue"),
            retention_basis=r_ev.get("basis"),
            primary_key=list(table_meta.primary_key) if table_meta else [],
            raw_type=(col_meta.raw_type if col_meta else "text"),
            nullable=(col_meta.nullable if col_meta else True),
            is_primary_key=(col_meta.is_primary_key if col_meta else False),
            is_foreign_key=(col_meta.is_foreign_key if col_meta else False),
            foreign_key_target=(col_meta.foreign_key_target if col_meta else None),
        )

    @classmethod
    def from_finding(cls, finding: dict, schema_ctx: dict, *, dialect="postgresql") -> "_Ctx":
        ev = finding.get("evidence") or {}
        s_ev, u_ev, r_ev = ev.get("sensitivity", {}), ev.get("usage", {}), ev.get("retention", {})
        pol = (r_ev.get("policy_applied") or {})
        b = finding.get("breakdown") or {}
        return cls(
            schema=schema_ctx.get("schema", "public"),
            table=finding["table"], column=finding["column"], dialect=dialect,
            verdict=finding.get("verdict", ""), necessity=finding.get("score", 0.0),
            sensitivity=finding.get("sensitivity", 0.0),
            usage=finding.get("usage", 0.0), retention=finding.get("retention", 0.0),
            thresholds={
                "sensitive": b.get("sensitivity", {}).get("threshold"),
                "unused": b.get("usage", {}).get("threshold"),
                "stale": b.get("staleness", {}).get("threshold"),
            },
            reasons=list(finding.get("reasons") or []),
            pii_type=s_ev.get("pii_type"), tier=s_ev.get("tier"),
            matched_rules=list(s_ev.get("matched_rules") or []),
            read_count=u_ev.get("read_count"), write_count=u_ev.get("write_count"),
            last_access_at=u_ev.get("last_access_at"),
            days_overdue=r_ev.get("days_overdue"),
            policy_days=pol.get("days"), anchor_column=pol.get("anchor"),
            oldest_row_age_days=r_ev.get("oldest_row_age_days"),
            fraction_overdue=r_ev.get("fraction_overdue"),
            retention_basis=r_ev.get("basis"),
            primary_key=list(schema_ctx.get("primary_key") or []),
            raw_type=schema_ctx.get("raw_type", "text"),
            nullable=bool(schema_ctx.get("nullable", True)),
            is_primary_key=bool(schema_ctx.get("is_primary_key")),
            is_foreign_key=bool(schema_ctx.get("is_foreign_key")),
            foreign_key_target=schema_ctx.get("foreign_key_target"),
        )


@dataclass
class ColumnRemediation:
    qualified_name: str
    strategy: str                      # archive_then_drop | anonymize_in_place | none
    summary: str
    sql: str
    cautions: list[str]
    reversible: bool
    dialect: str

    def to_dict(self) -> dict:
        return asdict(self)


class RemediationGenerator:
    def __init__(self, *, archive_schema: str = ARCHIVE_SCHEMA, now: Optional[datetime] = None):
        self.archive_schema = archive_schema
        self.now = now or datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #

    def generate(self, ns, col_meta=None, table_meta=None, **kw) -> ColumnRemediation:
        ctx = _Ctx.from_score(ns, col_meta, table_meta, **kw)
        return self._render(ctx)

    def generate_from_finding(self, finding: dict, schema_ctx: dict, **kw) -> ColumnRemediation:
        return self._render(_Ctx.from_finding(finding, schema_ctx, **kw))

    # ------------------------------------------------------------------ #

    def _render(self, c: _Ctx) -> ColumnRemediation:
        qn = f"{c.table}.{c.column}"
        if c.verdict not in ("flag_for_deletion", "review"):
            return ColumnRemediation(
                qualified_name=qn, strategy="none",
                summary="Actively used or not sensitive - no remediation suggested.",
                sql=f"-- {qn}: verdict '{c.verdict}'. No minimization action recommended.\n",
                cautions=[], reversible=True, dialect=c.dialect,
            )

        q = _quoter(c.dialect)
        droppable = not (c.is_primary_key or c.is_foreign_key)
        strategy = "archive_then_drop" if droppable else "anonymize_in_place"

        cautions = self._cautions(c, strategy)
        header = self._header(c, strategy)

        if strategy == "archive_then_drop":
            sql = header + self._archive_then_drop_sql(c, q)
            summary = f"Archive {c.column} to {self.archive_schema}, then DROP the column."
            reversible = True
        else:
            sql = header + self._anonymize_sql(c, q)
            summary = (f"{c.column} is a "
                       f"{'primary' if c.is_primary_key else 'foreign'} key - anonymize in place "
                       f"rather than drop.")
            reversible = False

        return ColumnRemediation(
            qualified_name=qn, strategy=strategy, summary=summary,
            sql=sql, cautions=cautions, reversible=reversible, dialect=c.dialect,
        )

    # ------------------------------------------------------------------ #

    def _header(self, c: _Ctx, strategy: str) -> str:
        d = self.now.strftime("%Y-%m-%d")
        t = c.thresholds or {}
        L = []
        L.append("-- " + "=" * 74)
        L.append(f"-- Remediation draft  --  {c.schema}.{c.table}.{c.column}")
        L.append(f"-- Generated {d} by the Automated Data Minimization Auditor.")
        L.append("-- DRAFT ONLY - review and run manually. Nothing here has been executed.")
        L.append("--")
        L.append(f"-- WHY FLAGGED  (necessity score {c.necessity:.3f}, verdict: {c.verdict})")
        L.append(f"--   Sensitivity {c.sensitivity:>5.2f}  (>= {t.get('sensitive')})"
                 f"  {c.pii_type or 'PII'}"
                 + (f", tier {c.tier}" if c.tier else ""))
        if c.matched_rules:
            L.append(f"--                     matched: {', '.join(c.matched_rules)}")
        r = c.read_count if c.read_count is not None else "?"
        w = c.write_count if c.write_count is not None else "?"
        L.append(f"--   Usage       {c.usage:>5.2f}  (<= {t.get('unused')})"
                 f"  {r} reads / {w} writes"
                 + (f" in the last {c.window_days}d" if c.window_days else " in the window"))
        if c.last_access_at:
            L.append(f"--                     last access: {c.last_access_at}")
        else:
            L.append("--                     no application query touched this column")
        L.append(f"--   Staleness   {c.retention:>5.2f}  (>= {t.get('stale')})")
        if c.days_overdue and c.policy_days:
            frac = f", {c.fraction_overdue:.0%} of rows overdue" if c.fraction_overdue is not None else ""
            L.append(f"--                     oldest row {c.oldest_row_age_days or '?'}d old - "
                     f"{c.days_overdue}d past the {c.policy_days}-day policy{frac}")
        if c.retention_basis and c.retention_basis.startswith("table:"):
            L.append(f"--                     (row age via {c.retention_basis.split(':',1)[1]})")
        L.append("--")
        for reason in c.reasons[:1]:
            L.append(f"-- {reason}")
        L.append("--")
        for cau in self._cautions(c, strategy):
            L.append(f"-- CAUTION: {cau}")
        L.append("-- " + "=" * 74)
        return "\n".join(L) + "\n\n"

    def _cautions(self, c: _Ctx, strategy: str) -> list[str]:
        out = ["This script has not been run. Execute it manually after review."]
        if c.pii_type in _HIGH_RETENTION_PII:
            out.append(f"{c.pii_type} data often has a statutory retention period - "
                       "confirm no legal/regulatory obligation to keep it before dropping.")
        else:
            out.append("Confirm no legal/contractual retention obligation for this data.")
        out.append("Take a fresh backup and run in a maintenance window.")
        if strategy == "archive_then_drop":
            if not c.nullable:
                out.append(f"{c.column} is NOT NULL - the archive copies only non-null rows; "
                           "the DROP removes the constraint with the column.")
            if c.fraction_overdue is not None and c.fraction_overdue < 0.999:
                out.append(f"{(1 - c.fraction_overdue):.0%} of rows are still within the retention "
                           "window; dropping the column removes it for those too. If the column "
                           "must be kept for recent rows, delete only the overdue rows instead "
                           "(see the ALTERNATIVE block).")
        else:
            out.append(f"{c.column} participates in a key"
                       + (f" (references {c.foreign_key_target})" if c.foreign_key_target else "")
                       + " - dropping it needs the constraint handled first; anonymization shown instead.")
        return out

    def _archive_then_drop_sql(self, c: _Ctx, q) -> str:
        d = self.now.strftime("%Y%m%d")
        arc = f"{c.table}__{c.column}__{d}"
        pk = c.primary_key or []
        key_cols = ", ".join(q(k) for k in pk) if pk else None
        select_cols = (key_cols + ", " if key_cols else "") + q(c.column)
        st = q(c.schema)
        L = []
        if c.dialect == "postgresql":
            L.append("BEGIN;")
            L.append("")
            L.append(f"-- 1. Preserve the data for legal hold "
                     + (f"(keyed by {key_cols})." if key_cols else "(no primary key - full-row copy)."))
            L.append(f"CREATE SCHEMA IF NOT EXISTS {q(self.archive_schema)};")
            if key_cols:
                L.append(f"CREATE TABLE {q(self.archive_schema)}.{q(arc)} AS")
                L.append(f"    SELECT {select_cols}")
                L.append(f"    FROM {st}.{q(c.table)}")
                L.append(f"    WHERE {q(c.column)} IS NOT NULL;")
            else:
                L.append(f"CREATE TABLE {q(self.archive_schema)}.{q(arc)} AS")
                L.append(f"    SELECT * FROM {st}.{q(c.table)} WHERE {q(c.column)} IS NOT NULL;")
            L.append("")
            L.append("-- 2. Drop the column.")
            L.append(f"ALTER TABLE {st}.{q(c.table)} DROP COLUMN {q(c.column)};")
            L.append("")
            L.append("COMMIT;")
            L.append("-- ROLLBACK;  -- run instead of COMMIT to abort")
            L.append("")
            L.append("-- RESTORE (after COMMIT):")
            L.append(f"--   ALTER TABLE {st}.{q(c.table)} ADD COLUMN {q(c.column)} {c.raw_type};")
            if key_cols and len(pk) == 1:
                k = q(pk[0])
                L.append(f"--   UPDATE {st}.{q(c.table)} t SET {q(c.column)} = a.{q(c.column)}")
                L.append(f"--     FROM {q(self.archive_schema)}.{q(arc)} a WHERE a.{k} = t.{k};")
            else:
                L.append(f"--   -- re-join {q(self.archive_schema)}.{q(arc)} on the primary key")
            L.append("")
            L.append("-- ALTERNATIVE - if only stale rows are the problem, delete those rows")
            L.append("-- (archive the full rows first), keeping the column for recent data:")
            if c.anchor_column and c.policy_days:
                L.append(f"--   CREATE TABLE {q(self.archive_schema)}.{q(c.table + '__overdue__' + d)} AS")
                L.append(f"--     SELECT * FROM {st}.{q(c.table)}")
                L.append(f"--     WHERE {q(c.anchor_column)} < now() - interval '{c.policy_days} days';")
                L.append(f"--   DELETE FROM {st}.{q(c.table)}")
                L.append(f"--     WHERE {q(c.anchor_column)} < now() - interval '{c.policy_days} days';")
            else:
                L.append("--   -- no retention anchor column identified for this table")
        else:  # mysql / other
            L.append("-- NOTE: DDL is not transactional on this engine - back up first.")
            L.append(f"CREATE TABLE {q(self.archive_schema + '_' + arc)} AS")
            L.append(f"    SELECT {select_cols} FROM {q(c.table)} WHERE {q(c.column)} IS NOT NULL;")
            L.append(f"ALTER TABLE {q(c.table)} DROP COLUMN {q(c.column)};")
        return "\n".join(L) + "\n"

    def _anonymize_sql(self, c: _Ctx, q) -> str:
        st = q(c.schema)
        d = self.now.strftime("%Y%m%d")
        arc = f"{c.table}__{c.column}__{d}"
        pk = c.primary_key or []
        key_cols = ", ".join(q(k) for k in pk) if pk else "*"
        is_texty = any(x in c.raw_type.lower() for x in ("char", "text", "citext", "json"))
        redaction = "NULL" if c.nullable else ("''" if is_texty else "0")
        L = []
        L.append("-- Column cannot be safely dropped (it is a key). Anonymize the values.")
        L.append(f"CREATE SCHEMA IF NOT EXISTS {q(self.archive_schema)};" if c.dialect == "postgresql" else "")
        L.append(f"CREATE TABLE {q(self.archive_schema)}.{q(arc)} AS")
        L.append(f"    SELECT {key_cols}{'' if key_cols == '*' else ', ' + q(c.column)} "
                 f"FROM {st}.{q(c.table)};")
        L.append("")
        if c.is_primary_key:
            L.append("-- WARNING: this column is the PRIMARY KEY. Overwriting it will break every")
            L.append("-- foreign key that references it. Pseudonymize keys across all tables in one")
            L.append("-- migration, or exclude this column from remediation. Manual review required.")
            L.append(f"-- UPDATE {st}.{q(c.table)} SET {q(c.column)} = /* pseudonymized id */;")
        else:
            L.append(f"UPDATE {st}.{q(c.table)} SET {q(c.column)} = {redaction};")
            if not c.nullable and redaction != "NULL":
                L.append(f"-- ({c.column} is NOT NULL - redacted to {redaction} instead of NULL)")
        return "\n".join(x for x in L if x != "") + "\n"


def _quoter(dialect: str):
    if dialect in ("mysql", "mariadb"):
        return lambda s: f"`{s}`"
    return lambda s: '"' + s.replace('"', '""') + '"'


def remediation_for_finding(finding: dict, schema_ctx: dict, **kw) -> ColumnRemediation:
    """Convenience: build a remediation from an API finding dict + its stored
    schema context (``evidence['schema']``)."""
    return RemediationGenerator(**{k: v for k, v in kw.items() if k in ("now", "archive_schema")}) \
        .generate_from_finding(finding, schema_ctx,
                               dialect=schema_ctx.get("dialect", "postgresql"))
