"""FastAPI app.

Endpoints
    POST /audits                              trigger a fresh audit run (persisted)
    GET  /audits                              list past runs (newest first)
    GET  /audits/{run_id}                     one run's ranked report (flagged first)
         ?flagged_only=true                   ... just the flagged columns
    GET  /audits/{run_id}/columns/{qname}     one column's full evidence
    GET  /audits/compare?base=&head=          what changed between two runs
    GET  /columns/{qname}/history             one column across every run
    GET  /health

`run_id` accepts the literal "latest".
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from auditor.api.schemas import (
    AuditRequest,
    ColumnEvidence,
    HistoryEntry,
    RunDiff,
    RunReport,
    RunSummary,
)
from auditor.scoring.pipeline import AuditRunConfig, run_audit
from auditor.storage import db as _db
from auditor.storage import repository as repo

@asynccontextmanager
async def lifespan(app: FastAPI):
    _db.init_db()
    yield


app = FastAPI(
    title="Automated Data Minimization Auditor",
    version="0.1.0",
    summary="Trigger minimization audits and browse their history.",
    lifespan=lifespan,
)

# Single-operator demo tool: allow the local dashboard (Vite dev server /
# preview build) to call the API directly when it isn't behind the dev proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "AUDIT_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173",
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "runs": len(repo.list_runs(limit=1000))}


@app.post("/audits", response_model=RunSummary, status_code=201)
def create_audit(req: AuditRequest) -> dict:
    try:
        run = run_audit(AuditRunConfig(
            metadata_path=req.metadata_path,
            log_path=req.log_path or AuditRunConfig().log_path,
            policy_path=req.policy_path,
            policy_days=req.policy_days,
            window_days=req.window_days,
            mode=req.mode,
            live_retention=req.live_retention,
        ))
    except FileNotFoundError as exc:
        raise HTTPException(400, f"input file not found: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface pipeline failures as 500 with detail
        raise HTTPException(500, f"audit pipeline failed: {type(exc).__name__}: {exc}")

    run_id = repo.record_run(run, note=req.note)
    summary, _ = repo.run_findings(run_id)
    return summary


@app.get("/audits", response_model=list[RunSummary])
def list_audits(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    return repo.list_runs(limit=limit)


@app.get("/audits/compare", response_model=RunDiff)
def compare_audits(base: str = Query(...), head: str = Query(...)) -> dict:
    try:
        return repo.diff_runs(base, head)
    except repo.RunNotFound as exc:
        raise HTTPException(404, str(exc))


@app.get("/audits/{run_id}", response_model=RunReport)
def get_audit(run_id: str, flagged_only: bool = Query(False)) -> dict:
    try:
        summary, findings = repo.run_findings(run_id, flagged_only=flagged_only)
    except repo.RunNotFound as exc:
        raise HTTPException(404, str(exc))
    return summary | {"columns": findings}


@app.get("/audits/{run_id}/columns/{qualified_name}", response_model=ColumnEvidence)
def get_column(run_id: str, qualified_name: str) -> dict:
    try:
        return repo.column_finding(run_id, qualified_name)
    except repo.RunNotFound as exc:
        raise HTTPException(404, str(exc))


@app.get("/columns/{qualified_name}/history", response_model=list[HistoryEntry])
def column_history(qualified_name: str, limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    hist = repo.column_history(qualified_name, limit=limit)
    if not hist:
        raise HTTPException(404, f"no recorded findings for {qualified_name}")
    return hist
