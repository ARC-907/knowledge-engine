"""Tests for the test/build/runtime log store (project_docs.logs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_engine.project_docs import schema
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.db import apply_migrations, connect
from knowledge_engine.project_docs.logs import (
    classify,
    get_failure_context,
    get_latest_test_summary,
    get_test_history,
    record_build_log,
    record_runtime_log,
    record_test_run,
)

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")
_PROJECT_FP = "proj_abc1234567890abc"
_BRANCH_FP = "br_def1234567890abcd"


@pytest.fixture()
def project_conn(tmp_path: Path):
    conn = connect(tmp_path / "proj.sqlite")
    apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    return conn


@pytest.fixture()
def registry_conn(tmp_path: Path):
    conn = connect(tmp_path / "fp.sqlite")
    apply_migrations(conn, only_prefixes=("001_",))
    return conn


def _cfg(*, retain_raw: bool = False) -> ProjectDocsConfig:
    if not retain_raw:
        return ProjectDocsConfig()
    from dataclasses import replace

    base = ProjectDocsConfig()
    return replace(base, ingestion=replace(base.ingestion, retain_raw_content=True))


# ── classify table ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exit_code", "output", "expected"),
    [
        (0, "all good", schema.PASS),
        (0, "", schema.PASS),
        (1, "1 failed", schema.FAIL),
        (2, "boom", schema.FAIL),
        (None, "Traceback (most recent call last):", schema.ERROR),
        (None, "ValueError: Error: bad", schema.ERROR),
        (None, "", schema.UNKNOWN),
        (None, "ran fine but no exit code", schema.UNKNOWN),
    ],
)
def test_classify_table(exit_code, output, expected):
    assert classify(exit_code, output) == expected


# ── record_test_run ───────────────────────────────────────────────────


def test_record_test_run_stores_run_and_sanitized_log(project_conn, registry_conn):
    run = record_test_run(
        project_conn,
        registry_conn,
        _cfg(),
        command="pytest -q",
        exit_code=0,
        started_at="2026-05-30T10:00:00",
        framework="pytest",
        target="tests/",
        output="2 passed in 0.10s",
        project_fp=_PROJECT_FP,
        branch_fp=_BRANCH_FP,
    )
    assert run.classification == schema.PASS
    assert run.raw_retained == 0

    row = project_conn.execute(
        "SELECT * FROM test_runs WHERE id = ?", (run.id,)
    ).fetchone()
    assert row is not None
    assert row["command"] == "pytest -q"
    assert row["classification"] == schema.PASS

    log = project_conn.execute(
        "SELECT * FROM test_log_records WHERE test_run_id = ?", (run.id,)
    ).fetchone()
    assert log is not None
    assert "passed" in log["sanitized_log"]


def test_record_test_run_omits_raw_by_default(project_conn, registry_conn):
    run = record_test_run(
        project_conn,
        registry_conn,
        _cfg(retain_raw=False),
        command="pytest",
        exit_code=1,
        started_at="2026-05-30T10:01:00",
        output="E   assert 1 == 2\n1 failed in 0.05s",
        project_fp=_PROJECT_FP,
        branch_fp=_BRANCH_FP,
    )
    assert run.classification == schema.FAIL
    assert run.failure_summary  # non-empty tail for a failing run
    log = project_conn.execute(
        "SELECT sanitized_log, raw_log FROM test_log_records WHERE test_run_id = ?",
        (run.id,),
    ).fetchone()
    assert log["raw_log"] is None
    assert log["sanitized_log"]


def test_record_test_run_retains_raw_when_enabled(project_conn, registry_conn):
    raw = "secret token=abc123 leaked\n1 failed"
    run = record_test_run(
        project_conn,
        registry_conn,
        _cfg(retain_raw=True),
        command="pytest",
        exit_code=1,
        started_at="2026-05-30T10:02:00",
        output=raw,
        project_fp=_PROJECT_FP,
        branch_fp=_BRANCH_FP,
    )
    assert run.raw_retained == 1
    log = project_conn.execute(
        "SELECT sanitized_log, raw_log FROM test_log_records WHERE test_run_id = ?",
        (run.id,),
    ).fetchone()
    assert log["raw_log"] == raw
    # Secret must be redacted in the sanitized copy even when raw is retained.
    assert "abc123" not in log["sanitized_log"]


# ── build / runtime logs ─────────────────────────────────────────────


def test_record_build_log_writes_row(project_conn, registry_conn):
    rec = record_build_log(
        project_conn,
        registry_conn,
        _cfg(),
        command="make build",
        exit_code=0,
        started_at="2026-05-30T11:00:00",
        output="Build succeeded",
        project_fp=_PROJECT_FP,
        branch_fp=_BRANCH_FP,
    )
    assert rec.classification == schema.PASS
    row = project_conn.execute(
        "SELECT * FROM build_log_records WHERE id = ?", (rec.id,)
    ).fetchone()
    assert row is not None
    assert row["raw_log"] is None
    assert row["sanitized_log"] == "Build succeeded"


def test_record_runtime_log_writes_row(project_conn, registry_conn):
    rec = record_runtime_log(
        project_conn,
        registry_conn,
        _cfg(),
        command="./app",
        exit_code=None,
        started_at="2026-05-30T11:05:00",
        output="Traceback (most recent call last):\nRuntimeError: x",
        project_fp=_PROJECT_FP,
        branch_fp=_BRANCH_FP,
    )
    assert rec.classification == schema.ERROR
    row = project_conn.execute(
        "SELECT * FROM runtime_log_records WHERE id = ?", (rec.id,)
    ).fetchone()
    assert row is not None


# ── history / latest / failure context ───────────────────────────────


def _seed_runs(project_conn, registry_conn):
    record_test_run(
        project_conn, registry_conn, _cfg(),
        command="pytest", exit_code=0, started_at="2026-05-30T09:00:00",
        output="ok", project_fp=_PROJECT_FP, branch_fp=_BRANCH_FP,
    )
    record_test_run(
        project_conn, registry_conn, _cfg(),
        command="pytest", exit_code=1, started_at="2026-05-30T09:30:00",
        output="1 failed", project_fp=_PROJECT_FP, branch_fp=_BRANCH_FP,
    )
    latest = record_test_run(
        project_conn, registry_conn, _cfg(),
        command="pytest", exit_code=0, started_at="2026-05-30T10:00:00",
        output="2 passed", project_fp=_PROJECT_FP, branch_fp=_BRANCH_FP,
    )
    return latest


def test_history_orders_newest_first(project_conn, registry_conn):
    latest = _seed_runs(project_conn, registry_conn)
    history = get_test_history(project_conn, limit=20)
    assert len(history) == 3
    assert history[0].id == latest.id
    assert history[0].started_at == "2026-05-30T10:00:00"
    # Descending order.
    times = [h.started_at for h in history]
    assert times == sorted(times, reverse=True)


def test_history_filters_by_branch(project_conn, registry_conn):
    _seed_runs(project_conn, registry_conn)
    record_test_run(
        project_conn, registry_conn, _cfg(),
        command="pytest", exit_code=0, started_at="2026-05-30T12:00:00",
        output="ok", project_fp=_PROJECT_FP, branch_fp="br_other000000000000",
    )
    only_main = get_test_history(project_conn, branch_fp=_BRANCH_FP)
    assert len(only_main) == 3
    assert all(h.branch_fp == _BRANCH_FP for h in only_main)


def test_latest_summary_returns_most_recent(project_conn, registry_conn):
    latest = _seed_runs(project_conn, registry_conn)
    summary = get_latest_test_summary(project_conn)
    assert summary is not None
    assert summary["id"] == latest.id
    assert summary["classification"] == schema.PASS


def test_latest_summary_none_when_empty(project_conn):
    assert get_latest_test_summary(project_conn) is None


def test_failure_context_returns_log(project_conn, registry_conn):
    run = record_test_run(
        project_conn, registry_conn, _cfg(),
        command="pytest", exit_code=1, started_at="2026-05-30T13:00:00",
        output="E   assert False\n1 failed in 0.02s",
        project_fp=_PROJECT_FP, branch_fp=_BRANCH_FP,
    )
    ctx = get_failure_context(project_conn, run.id)
    assert ctx["id"] == run.id
    assert ctx["classification"] == schema.FAIL
    assert "failed" in ctx["sanitized_log"]
    assert ctx["failure_summary"]


def test_failure_context_unknown_id(project_conn):
    assert get_failure_context(project_conn, "nope") == {}
