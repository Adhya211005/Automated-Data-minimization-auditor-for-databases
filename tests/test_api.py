"""Phase 6 - FastAPI backend + SQLite audit history.

Each test module gets a fresh temp history DB. The pipeline runs offline from
a metadata dump + the Phase 0 query log, so these tests don't need Postgres
beyond the one-time metadata extraction.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def meta_file(metadata, tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("api") / "metadata.json"
    metadata.save(p)
    return str(p)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_HISTORY_DB", str(tmp_path / "history.db"))
    from auditor.storage.db import reset_engine

    reset_engine()
    from auditor.api.main import app

    with TestClient(app) as c:
        yield c
    reset_engine()


def _audit_body(meta_file, **over):
    body = {
        "metadata_path": meta_file,
        "log_path": "seed-data/output/query_log.jsonl",
        "policy_days": 365,
        "window_days": 90,
    }
    body.update(over)
    return body


# --- endpoint 1: trigger a run -------------------------------------- #

def test_create_audit_runs_and_persists(client, meta_file):
    r = client.post("/audits", json=_audit_body(meta_file, note="first run"))
    assert r.status_code == 201
    s = r.json()
    assert s["n_columns"] == 46
    assert s["n_flagged"] == 2
    assert s["verdict_counts"]["flag_for_deletion"] == 2
    assert s["note"] == "first run"
    assert s["thresholds"] == {"sensitive": 0.5, "unused": 0.2, "stale": 0.5}

    assert client.get("/audits").json()[0]["id"] == s["id"]
    assert client.get("/health").json() == {"status": "ok", "runs": 1}


# --- endpoint 2: latest ranked report ----------------------------- #

def test_get_report_flagged_first_with_breakdown(client, meta_file):
    rid = client.post("/audits", json=_audit_body(meta_file)).json()["id"]

    full = client.get(f"/audits/{rid}").json()
    assert len(full["columns"]) == 46
    assert full["columns"][0]["qualified_name"] == "users.mothers_maiden_name"
    assert full["columns"][1]["qualified_name"] == "users.ssn"
    assert [c["is_flagged"] for c in full["columns"][:2]] == [True, True]
    top = full["columns"][0]
    assert top["breakdown"]["formula"] == "sensitivity * (1 - usage) * staleness"
    assert top["reasons"]

    latest = client.get("/audits/latest").json()
    assert latest["id"] == rid

    flagged = client.get(f"/audits/{rid}?flagged_only=true").json()
    assert {c["qualified_name"] for c in flagged["columns"]} == {
        "users.mothers_maiden_name", "users.ssn"}


# --- endpoint 3: single column evidence -------------------------- #

def test_get_column_evidence(client, meta_file):
    rid = client.post("/audits", json=_audit_body(meta_file)).json()["id"]

    r = client.get(f"/audits/{rid}/columns/users.mothers_maiden_name")
    assert r.status_code == 200
    j = r.json()
    assert j["verdict"] == "flag_for_deletion"
    assert (j["is_sensitive"], j["is_unused"], j["is_stale"]) == (True, True, True)
    assert set(j["evidence"]) == {"sensitivity", "usage", "retention"}
    assert j["evidence"]["usage"]["read_count"] == 0
    assert j["evidence"]["retention"]["days_overdue"] > 400
    assert any("minimization violation" in reason for reason in j["reasons"])

    # a kept column
    email = client.get(f"/audits/{rid}/columns/users.email").json()
    assert email["verdict"] == "keep" and email["is_flagged"] is False


def test_not_found_cases(client, meta_file):
    client.post("/audits", json=_audit_body(meta_file))
    assert client.get("/audits/does-not-exist").status_code == 404
    assert client.get("/audits/latest/columns/users.nope").status_code == 404
    assert client.get("/columns/users.nope/history").status_code == 404


# --- remediation drafts ------------------------------------------- #

def test_flagged_column_carries_a_remediation_draft(client, meta_file):
    rid = client.post("/audits", json=_audit_body(meta_file)).json()["id"]

    col = client.get(f"/audits/{rid}/columns/users.mothers_maiden_name").json()
    assert col["has_remediation"] is True
    rem = col["remediation"]
    assert rem["strategy"] == "archive_then_drop"
    assert rem["reversible"] is True
    assert "DROP COLUMN" in rem["sql"] and "dma_archive" in rem["sql"]
    assert "WHY FLAGGED" in rem["sql"]           # evidence embedded as comments
    assert any("legal" in c.lower() or "retention" in c.lower() for c in rem["cautions"])

    # dedicated endpoint
    direct = client.get(f"/audits/{rid}/columns/users.mothers_maiden_name/remediation")
    assert direct.status_code == 200
    assert direct.json()["sql"] == rem["sql"]

    # nothing was executed - the draft only mentions the archive schema, doesn't create it
    assert "dma_archive" not in client.get(f"/audits/{rid}").json()["target"]


def test_no_remediation_for_kept_columns(client, meta_file):
    rid = client.post("/audits", json=_audit_body(meta_file)).json()["id"]
    email = client.get(f"/audits/{rid}/columns/users.email").json()
    assert email["has_remediation"] is False
    assert email["remediation"] is None
    assert client.get(f"/audits/{rid}/columns/users.email/remediation").status_code == 404


def test_ssn_draft_flags_statutory_retention(client, meta_file):
    rid = client.post("/audits", json=_audit_body(meta_file)).json()["id"]
    rem = client.get(f"/audits/{rid}/columns/users.ssn/remediation").json()
    assert any("statutory" in c for c in rem["cautions"])
    assert 'DROP COLUMN "ssn"' in rem["sql"]


# --- history + comparison (the DPO workflow) --------------------- #

def test_column_history_across_runs(client, meta_file):
    client.post("/audits", json=_audit_body(meta_file, note="run 1"))
    client.post("/audits", json=_audit_body(meta_file, note="run 2"))

    hist = client.get("/columns/users.mothers_maiden_name/history").json()
    assert len(hist) == 2
    assert all(h["is_flagged"] for h in hist)          # "flagged last month too"
    assert all(h["verdict"] == "flag_for_deletion" for h in hist)


def test_compare_detects_threshold_crossing(client, meta_file):
    base = client.post("/audits", json=_audit_body(meta_file, policy_days=365)).json()["id"]
    # a much longer policy -> users no longer stale -> nothing flagged
    head = client.post("/audits", json=_audit_body(meta_file, policy_days=5000)).json()["id"]

    d = client.get(f"/audits/compare?base={base}&head={head}").json()
    assert d["n_flagged_delta"] == -2
    no_longer = {x["qualified_name"] for x in d["no_longer_flagged"]}
    assert no_longer == {"users.mothers_maiden_name", "users.ssn"}
    changed = {x["qualified_name"]: (x["from"], x["to"]) for x in d["verdict_changed"]}
    assert changed["users.mothers_maiden_name"] == ("flag_for_deletion", "review")


# --- the explicit ask: reruns are consistent -------------------- #

def test_second_run_on_unchanged_data_is_identical(client, meta_file):
    r1 = client.post("/audits", json=_audit_body(meta_file)).json()["id"]
    r2 = client.post("/audits", json=_audit_body(meta_file)).json()["id"]
    assert r1 != r2

    a = {c["qualified_name"]: c for c in client.get(f"/audits/{r1}").json()["columns"]}
    b = {c["qualified_name"]: c for c in client.get(f"/audits/{r2}").json()["columns"]}
    assert a.keys() == b.keys()
    for qn in a:
        assert a[qn]["score"] == b[qn]["score"], qn
        assert a[qn]["verdict"] == b[qn]["verdict"], qn
        assert a[qn]["is_flagged"] == b[qn]["is_flagged"], qn

    d = client.get(f"/audits/compare?base={r1}&head={r2}").json()
    assert d["n_flagged_delta"] == 0
    assert d["newly_flagged"] == d["no_longer_flagged"] == d["verdict_changed"] == []
    assert d["score_moved"] == []
