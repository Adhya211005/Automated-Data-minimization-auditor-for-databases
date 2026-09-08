"""Read/write helpers over the audit-history tables."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select

from auditor.remediation.generator import RemediationGenerator
from auditor.scoring.pipeline import AuditRun
from auditor.storage.db import get_session
from auditor.storage.models import AuditRunRow, ColumnFindingRow

_REMEDIATION_VERDICTS = {"flag_for_deletion", "review"}


class RunNotFound(LookupError):
    pass


def record_run(run: AuditRun, *, note: str = "") -> str:
    """Persist an AuditRun and its findings. Returns the new run id."""
    run_id = str(uuid.uuid4())
    res = run.result
    remgen = RemediationGenerator()
    with get_session() as s:
        row = AuditRunRow(
            id=run_id,
            started_at=run.started_at,
            finished_at=run.finished_at,
            target=run.target,
            window_days=run.config.window_days,
            mode=run.config.mode,
            policy=run.policy.to_dict(),
            thresholds=res.thresholds,
            n_columns=res.n_columns,
            n_flagged=res.n_flagged,
            verdict_counts=res.verdict_counts,
            duration_seconds=run.duration_seconds,
            note=note,
        )
        for rank, ns in enumerate(res.columns, 1):
            remediation = None
            if ns.verdict in _REMEDIATION_VERDICTS:
                remediation = remgen.generate(
                    ns,
                    run.metadata.column(ns.qualified_name),
                    run.metadata.table(ns.table),
                ).to_dict()
            row.findings.append(ColumnFindingRow(
                rank=rank,
                qualified_name=ns.qualified_name,
                table_name=ns.table,
                column_name=ns.column,
                score=ns.score,
                is_flagged=ns.is_flagged,
                verdict=ns.verdict,
                sensitivity=ns.sensitivity,
                usage=ns.usage,
                retention=ns.retention,
                is_sensitive=ns.is_sensitive,
                is_unused=ns.is_unused,
                is_stale=ns.is_stale,
                breakdown=ns.breakdown,
                reasons=ns.reasons,
                evidence=ns.evidence,
                remediation=remediation,
            ))
        s.add(row)
    return run_id


def _resolve_run_id(s, run_id: str) -> str:
    if run_id in ("latest", "last"):
        rid = s.execute(
            select(AuditRunRow.id).order_by(AuditRunRow.finished_at.desc()).limit(1)
        ).scalar_one_or_none()
        if rid is None:
            raise RunNotFound("no audit runs recorded yet")
        return rid
    return run_id


def get_run(run_id: str) -> dict:
    with get_session() as s:
        rid = _resolve_run_id(s, run_id)
        row = s.get(AuditRunRow, rid)
        if row is None:
            raise RunNotFound(run_id)
        return _run_to_dict(row)


def list_runs(limit: int = 50) -> list[dict]:
    with get_session() as s:
        rows = s.execute(
            select(AuditRunRow).order_by(AuditRunRow.finished_at.desc()).limit(limit)
        ).scalars().all()
        return [_run_summary(r) for r in rows]


def run_findings(run_id: str, *, flagged_only: bool = False) -> tuple[dict, list[dict]]:
    with get_session() as s:
        rid = _resolve_run_id(s, run_id)
        row = s.get(AuditRunRow, rid)
        if row is None:
            raise RunNotFound(run_id)
        findings = [f for f in row.findings if (f.is_flagged or not flagged_only)]
        return _run_summary(row), [_finding_to_dict(f) for f in findings]


def column_finding(run_id: str, qualified_name: str) -> dict:
    with get_session() as s:
        rid = _resolve_run_id(s, run_id)
        f = s.execute(
            select(ColumnFindingRow).where(
                ColumnFindingRow.run_id == rid,
                ColumnFindingRow.qualified_name == qualified_name,
            )
        ).scalar_one_or_none()
        if f is None:
            raise RunNotFound(f"{qualified_name} in run {rid}")
        return _finding_to_dict(f, full=True)


def column_history(qualified_name: str, *, limit: int = 50) -> list[dict]:
    """Every recorded finding for one column, newest run first - the DPO's
    'was this flagged last month too?' view."""
    with get_session() as s:
        rows = s.execute(
            select(ColumnFindingRow, AuditRunRow)
            .join(AuditRunRow, ColumnFindingRow.run_id == AuditRunRow.id)
            .where(ColumnFindingRow.qualified_name == qualified_name)
            .order_by(AuditRunRow.finished_at.desc())
            .limit(limit)
        ).all()
        out = []
        for f, r in rows:
            out.append({
                "run_id": r.id,
                "finished_at": _iso(r.finished_at),
                "score": f.score,
                "is_flagged": f.is_flagged,
                "verdict": f.verdict,
                "sensitivity": f.sensitivity,
                "usage": f.usage,
                "retention": f.retention,
            })
        return out


def diff_runs(base_id: str, head_id: str) -> dict:
    """What changed between two runs - newly flagged, no longer flagged,
    verdict changes, score movement."""
    base_summary, base_f = run_findings(base_id)
    head_summary, head_f = run_findings(head_id)
    base = {f["qualified_name"]: f for f in base_f}
    head = {f["qualified_name"]: f for f in head_f}

    newly_flagged, no_longer_flagged, verdict_changed, score_moved = [], [], [], []
    for qn, h in head.items():
        b = base.get(qn)
        if b is None:
            if h["is_flagged"]:
                newly_flagged.append({"qualified_name": qn, "verdict": h["verdict"], "added": True})
            continue
        if h["is_flagged"] and not b["is_flagged"]:
            newly_flagged.append(_delta(qn, b, h))
        if b["is_flagged"] and not h["is_flagged"]:
            no_longer_flagged.append(_delta(qn, b, h))
        if b["verdict"] != h["verdict"]:
            verdict_changed.append({"qualified_name": qn, "from": b["verdict"], "to": h["verdict"]})
        if abs(h["score"] - b["score"]) >= 0.05:
            score_moved.append(_delta(qn, b, h))

    for qn, b in base.items():
        if qn not in head and b["is_flagged"]:
            no_longer_flagged.append({"qualified_name": qn, "verdict": b["verdict"], "removed": True})

    score_moved.sort(key=lambda d: -abs(d["score_delta"]))
    return {
        "base": base_summary,
        "head": head_summary,
        "n_flagged_delta": head_summary["n_flagged"] - base_summary["n_flagged"],
        "newly_flagged": newly_flagged,
        "no_longer_flagged": no_longer_flagged,
        "verdict_changed": verdict_changed,
        "score_moved": score_moved,
    }


# --- serializers --------------------------------------------------- #

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _run_summary(r: AuditRunRow) -> dict:
    return {
        "id": r.id,
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "target": r.target,
        "window_days": r.window_days,
        "mode": r.mode,
        "policy": r.policy,
        "thresholds": r.thresholds,
        "n_columns": r.n_columns,
        "n_flagged": r.n_flagged,
        "verdict_counts": r.verdict_counts,
        "duration_seconds": r.duration_seconds,
        "note": r.note,
    }


def _run_to_dict(r: AuditRunRow) -> dict:
    d = _run_summary(r)
    d["columns"] = [_finding_to_dict(f) for f in r.findings]
    return d


def _finding_to_dict(f: ColumnFindingRow, *, full: bool = False) -> dict:
    d = {
        "rank": f.rank,
        "qualified_name": f.qualified_name,
        "table": f.table_name,
        "column": f.column_name,
        "score": f.score,
        "is_flagged": f.is_flagged,
        "verdict": f.verdict,
        "sensitivity": f.sensitivity,
        "usage": f.usage,
        "retention": f.retention,
        "is_sensitive": f.is_sensitive,
        "is_unused": f.is_unused,
        "is_stale": f.is_stale,
        "breakdown": f.breakdown,
        "reasons": f.reasons,
        "has_remediation": f.remediation is not None,
    }
    if full:
        d["evidence"] = f.evidence
        d["remediation"] = f.remediation
    return d


def column_remediation(run_id: str, qualified_name: str) -> dict:
    with get_session() as s:
        rid = _resolve_run_id(s, run_id)
        f = s.execute(
            select(ColumnFindingRow).where(
                ColumnFindingRow.run_id == rid,
                ColumnFindingRow.qualified_name == qualified_name,
            )
        ).scalar_one_or_none()
        if f is None:
            raise RunNotFound(f"{qualified_name} in run {rid}")
        if f.remediation is None:
            raise RunNotFound(
                f"no remediation for {qualified_name} (verdict '{f.verdict}' - "
                "drafts are generated for flag_for_deletion and review only)"
            )
        return f.remediation


def _delta(qn: str, b: dict, h: dict) -> dict:
    return {
        "qualified_name": qn,
        "verdict": h["verdict"],
        "verdict_was": b["verdict"],
        "score": h["score"],
        "score_delta": round(h["score"] - b["score"], 4),
    }
