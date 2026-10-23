"""Tests for the optional git-context collector.

All tests are offline. Git subprocess calls are either pointed at a non-repo
temp dir (so ``git`` reports failure without network) or monkeypatched to raise
``FileNotFoundError`` (simulating a missing binary). Numstat parsing is tested
through the pure ``_parse_numstat`` helper so no git invocation is required.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_context import db, git_context
from knowledge_engine.project_context.config import ProjectContextConfig
from knowledge_engine.project_context.models import DiffSummary, GitContext

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


# ── collect: graceful degradation ────────────────────────────────────


def test_collect_returns_none_when_not_a_repo(tmp_path):
    """A plain temp dir is not a git work tree → collect returns None."""
    cfg = ProjectContextConfig()
    assert git_context.collect(tmp_path, cfg) is None


def test_collect_returns_none_when_git_binary_missing(tmp_path, monkeypatch):
    """If the git binary is absent, subprocess.run raises FileNotFoundError;
    collect must swallow it and return None."""

    def _boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(git_context.subprocess, "run", _boom)
    assert git_context.collect(tmp_path, cfg=ProjectContextConfig()) is None


def test_collect_returns_none_when_git_disabled(tmp_path, monkeypatch):
    """When cfg.git.enabled is False, collect returns None without touching
    subprocess at all."""

    def _should_not_run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when git disabled")

    monkeypatch.setattr(git_context.subprocess, "run", _should_not_run)

    base = ProjectContextConfig()
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


# ── validate_ref: strict, local, no subprocess ───────────────────────


@pytest.mark.parametrize(
    "ref",
    [
        "HEAD",
        "HEAD~3",
        "HEAD^",
        "HEAD^2~1",
        "main",
        "origin/main",
        "feature/some-thing_v2",
        "v1.2.3",
        "a" * 40,
        "0123456789abcdef0123456789abcdef01234567",
        "deadbee",
    ],
)
def test_validate_ref_accepts_ordinary_refs(ref: str) -> None:
    assert git_context.validate_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "-",
        "--output=/tmp/x",
        "-p",
        "--end-of-options",
        "..",
        "main..HEAD",
        "main...HEAD",
        "HEAD@{1}",
        "@",
        "main\\evil",
        "with space",
        "tab\there",
        "new\nline",
        "nul\x00byte",
        "del\x7f",
        "main:path",
        "glob*",
        "what?",
        "br[ack",
        ".hidden",
        "refs/.hidden",
        "ends.lock",
        "refs/heads/x.lock",
        "trailing.",
        "trailing/",
        "/leading",
        "double//slash",
        "HEAD^{commit}",
        "HEAD~x",
        "x" * 257,
    ],
)
def test_validate_ref_rejects_unsafe_refs(ref: str) -> None:
    with pytest.raises(ValueError, match="git ref"):
        git_context.validate_ref(ref)


@pytest.mark.parametrize("ref", [None, 7, b"main", ["main"]])
def test_validate_ref_rejects_non_strings(ref: object) -> None:
    with pytest.raises(ValueError, match="must be a string"):
        git_context.validate_ref(ref)


# ── run_git / diff_summary argv shape (monkeypatched runner) ─────────


class _Recorder:
    """Stand-in for :func:`git_context.run_git` that records every argv."""

    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, args, cwd, *, timeout=git_context.GIT_TIMEOUT_SECONDS):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(["git", *args], 0, stdout=self.stdout, stderr="")


def test_run_git_uses_argv_list_without_shell(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(git_context.subprocess, "run", _fake_run)
    proc = git_context.run_git(["status", "--porcelain"], tmp_path, timeout=3)
    assert proc.stdout == "ok\n"
    assert seen["argv"] == ["git", "status", "--porcelain"]
    assert seen["shell"] is False
    assert seen["check"] is False
    assert seen["timeout"] == 3
    assert seen["cwd"] == str(tmp_path)


def test_diff_summary_places_refs_between_option_terminators(tmp_path: Path, monkeypatch) -> None:
    rec = _Recorder(stdout="3\t1\tsrc/a.py\n")
    monkeypatch.setattr(git_context, "run_git", rec)
    result = git_context.diff_summary(tmp_path, "HEAD~1", "HEAD")
    assert rec.calls == [["diff", "--numstat", "--end-of-options", "HEAD~1", "HEAD", "--"]]
    argv = rec.calls[0]
    assert argv.index("--end-of-options") < argv.index("HEAD~1") < argv.index("--")
    assert result.files_changed == 1
    assert result.insertions == 3


@pytest.mark.parametrize(
    ("a", "b"),
    [("--output=/tmp/x", "HEAD"), ("HEAD~1", "--output=/tmp/x"), ("-R", "HEAD")],
)
def test_diff_summary_rejects_option_refs_before_running_git(
    tmp_path: Path, monkeypatch, a: str, b: str
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    with pytest.raises(ValueError, match="must not start with '-'"):
        git_context.diff_summary(tmp_path, a, b)
    assert rec.calls == []


# ── real git (offline, local repo) ───────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not installed")
def test_diff_summary_output_injection_creates_no_file(tmp_path: Path) -> None:
    """``--output=<path>`` would make ``git diff`` write a file; it must be rejected."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "one")
    (repo / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "two")

    target = tmp_path / "pwned.txt"
    with pytest.raises(ValueError):
        git_context.diff_summary(repo, f"--output={target}", "HEAD")
    assert not target.exists()

    ok = git_context.diff_summary(repo, "HEAD~1", "HEAD")
    assert (ok.files_changed, ok.insertions, ok.deletions) == (1, 1, 0)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
