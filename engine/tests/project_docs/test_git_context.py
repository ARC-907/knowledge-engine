"""Tests for the optional git-context collector.

All tests are offline. Git subprocess calls are either pointed at a non-repo
temp dir (so ``git`` reports failure without network) or monkeypatched to raise
``FileNotFoundError`` (simulating a missing binary). Numstat parsing is tested
through the pure ``_parse_numstat`` helper so no git invocation is required.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from knowledge_engine.project_docs import db, git_context
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.models import DiffSummary, GitContext

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


# ── collect: graceful degradation ────────────────────────────────────


def test_collect_returns_none_when_not_a_repo(tmp_path):
    """A plain temp dir is not a git work tree → collect returns None."""
    cfg = ProjectDocsConfig()
    assert git_context.collect(tmp_path, cfg) is None


def test_collect_returns_none_when_git_binary_missing(tmp_path, monkeypatch):
    """If the git binary is absent, subprocess.run raises FileNotFoundError;
    collect must swallow it and return None."""

    def _boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(git_context.subprocess, "run", _boom)
    assert git_context.collect(tmp_path, cfg=ProjectDocsConfig()) is None


def test_collect_returns_none_when_git_disabled(tmp_path, monkeypatch):
    """When cfg.git.enabled is False, collect returns None without touching
    subprocess at all."""

    def _should_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when git disabled")

    monkeypatch.setattr(git_context.subprocess, "run", _should_not_run)

    base = ProjectDocsConfig()
    cfg = replace(base, git=replace(base.git, enabled=False))
    assert git_context.collect(tmp_path, cfg) is None


# ── numstat parsing (pure helper) ────────────────────────────────────


def test_parse_numstat_basic():
    sample = "3\t1\tsrc/a.py\n10\t0\tsrc/b.py\n0\t4\tREADME.md\n"
    files_changed, insertions, deletions = git_context._parse_numstat(sample)
    assert files_changed == 3
    assert insertions == 13
    assert deletions == 5


def test_parse_numstat_binary_and_blank_lines():
    """Binary files report '-' for counts: they count as changed files but add
    nothing to line totals. Blank lines are ignored."""
    sample = "-\t-\tassets/logo.png\n\n2\t2\tsrc/c.py\n"
    files_changed, insertions, deletions = git_context._parse_numstat(sample)
    assert files_changed == 2
    assert insertions == 2
    assert deletions == 2


def test_parse_numstat_empty():
    assert git_context._parse_numstat("") == (0, 0, 0)


# ── remote sanitization (no raw URL leaks) ───────────────────────────


def test_sanitize_remote_strips_credentials_https():
    out = git_context._sanitize_remote("https://user:token@github.com/acme/repo.git")
    assert out == "github.com/acme/repo"
    assert "token" not in out
    assert "user" not in out


def test_sanitize_remote_scp_form():
    out = git_context._sanitize_remote("git@github.com:acme/repo.git")
    assert out == "github.com/acme/repo"


# ── diff_summary: graceful degradation ───────────────────────────────


def test_diff_summary_unavailable_when_not_a_repo(tmp_path):
    result = git_context.diff_summary(tmp_path, "HEAD~1", "HEAD")
    assert isinstance(result, DiffSummary)
    assert result.files_changed == 0
    assert result.insertions == 0
    assert result.deletions == 0
    assert result.from_ref == "HEAD~1"
    assert result.to_ref == "HEAD"


def test_diff_summary_unavailable_when_git_missing(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(git_context.subprocess, "run", _boom)
    result = git_context.diff_summary(tmp_path, "A", "B")
    assert result.files_changed == 0
    assert result.summary == "git diff unavailable"


# ── store_git_context: persists to the project DB ────────────────────


def _project_conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    return conn


def test_store_git_context_inserts_row():
    conn = _project_conn()
    gctx = GitContext(
        branch="main",
        commit_hash="abc123",
        dirty=True,
        remote_hash="deadbeef",
        data={"note": "x"},
    )
    git_context.store_git_context(conn, "proj_aaa", "br_bbb", gctx)

    row = conn.execute(
        "SELECT project_fp, branch_fp, branch, commit_hash, dirty, remote_hash, data_json "
        "FROM git_context"
    ).fetchone()
    assert row["project_fp"] == "proj_aaa"
    assert row["branch_fp"] == "br_bbb"
    assert row["branch"] == "main"
    assert row["commit_hash"] == "abc123"
    assert row["dirty"] == 1
    assert row["remote_hash"] == "deadbeef"
    assert '"note"' in row["data_json"]
    conn.close()


def test_store_git_context_defaults_data_json():
    conn = _project_conn()
    gctx = GitContext(branch=None, commit_hash=None, dirty=False)
    git_context.store_git_context(conn, "proj_x", "br_y", gctx)

    row = conn.execute("SELECT dirty, data_json FROM git_context").fetchone()
    assert row["dirty"] == 0
    assert row["data_json"] == "{}"
    conn.close()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
