"""Tests for the git/lineage MCP tool module.

These tests are offline: they never invoke real git over the network and rely on
config gates rather than the presence of a repository. The key behaviors:

* ``project_context.git_context`` returns ``disabled`` when ``git.enabled`` is False.
* ``project_context.search_by_diff`` is disabled by default (diff summaries off).
* ``project_context.search_by_commit`` returns a clean empty list against an empty
  project DB.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_context import db as pddb, git_context
from knowledge_engine.project_context.config import ProjectContextConfig
from knowledge_engine.project_context.mcp_tools import git_tools
from knowledge_engine.project_context.mcp_tools.base import ToolContext

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


def _payload(result: dict) -> object:
    """Decode the JSON object embedded in an MCP text-content envelope."""
    return json.loads(result["content"][0]["text"])


def _enabled_cfg() -> ProjectContextConfig:
    """A config with git enabled but diff summaries still off (the default)."""
    base = ProjectContextConfig()
    return replace(base, enabled=True, git=replace(base.git, enabled=True))


def _ctx(cfg: ProjectContextConfig, tmp_path: Path, project_conn=None) -> ToolContext:
    """Build a ToolContext, optionally pre-wiring a project connection so the
    tools never touch the real filesystem registry."""
    ctx = ToolContext(cfg=cfg, root=tmp_path)
    if project_conn is not None:
        ctx._projects[ctx.project_slug_for(None)] = project_conn
    return ctx


def _project_conn(tmp_path: Path):
    """Create an in-memory-style project DB on disk with migrations applied."""
    conn = pddb.connect(tmp_path / "proj.sqlite")
    pddb.apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    return conn


# ── group contract ───────────────────────────────────────────────────


def test_group_name() -> None:
    assert git_tools.GROUP == "git"


def test_tools_listed() -> None:
    names = {t["name"] for t in git_tools.tools(_enabled_cfg())}
    assert names == {
        "project_context.git_context",
        "project_context.search_by_commit",
        "project_context.get_branch_lineage",
        "project_context.get_change_context",
        "project_context.explain_file_history",
        "project_context.search_by_diff",
    }


# ── gating ───────────────────────────────────────────────────────────


def test_git_context_disabled_when_git_off(tmp_path: Path) -> None:
    cfg = ProjectContextConfig(enabled=True)  # git.enabled defaults True...
    cfg = replace(cfg, git=replace(cfg.git, enabled=False))
    ctx = _ctx(cfg, tmp_path)
    result = git_tools.dispatch("project_context.git_context", {}, ctx)
    assert _payload(result)["status"] == "disabled"


def test_search_by_diff_disabled_by_default(tmp_path: Path) -> None:
    # git enabled, but include_diff_summaries is off (the conservative default).
    cfg = _enabled_cfg()
    assert cfg.git.include_diff_summaries is False
    ctx = _ctx(cfg, tmp_path)
    result = git_tools.dispatch(
        "project_context.search_by_diff", {"from_ref": "HEAD~1", "to_ref": "HEAD"}, ctx
    )
    assert _payload(result)["status"] == "disabled"


def test_diff_gated_tools_disabled_when_git_off(tmp_path: Path) -> None:
    cfg = ProjectContextConfig(enabled=True)
    cfg = replace(cfg, git=replace(cfg.git, enabled=False))
    ctx = _ctx(cfg, tmp_path)
    for tool in (
        "project_context.get_branch_lineage",
        "project_context.get_change_context",
        "project_context.explain_file_history",
        "project_context.search_by_diff",
    ):
        result = git_tools.dispatch(tool, {"from_ref": "a", "path": "x", "commit": "y"}, ctx)
        assert _payload(result)["status"] == "disabled", tool


# ── search_by_commit on an empty project DB ──────────────────────────


def test_search_by_commit_empty_project(tmp_path: Path) -> None:
    conn = _project_conn(tmp_path)
    ctx = _ctx(_enabled_cfg(), tmp_path, project_conn=conn)
    result = git_tools.dispatch("project_context.search_by_commit", {"commit": "deadbeef"}, ctx)
    payload = _payload(result)
    assert payload == []


def test_search_by_commit_unknown_project_returns_empty(tmp_path: Path) -> None:
    # An unknown project_fp -> project_conn returns None -> clean empty list.
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch(
        "project_context.search_by_commit",
        {"commit": "abc123", "project_fp": "proj_doesnotexist0"},
        ctx,
    )
    assert _payload(result) == []


def test_search_by_commit_requires_commit(tmp_path: Path) -> None:
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_context.search_by_commit", {}, ctx)
    assert _payload(result)["status"] == "invalid_args"


# ── unknown tool ─────────────────────────────────────────────────────


def test_unknown_tool(tmp_path: Path) -> None:
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_context.nope", {}, ctx)
    assert _payload(result)["status"] == "unknown_tool"


# ── git-absent degradation (mocked, offline) ─────────────────────────


def test_git_context_not_configured_when_not_repo(tmp_path: Path, monkeypatch) -> None:
    # Force "not a repo" without touching real git.
    monkeypatch.setattr(git_tools.gitctx, "_is_repo", lambda root: False)
    monkeypatch.setattr(git_tools.gitctx, "collect", lambda root, cfg: None)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_context.git_context", {}, ctx)
    assert _payload(result)["status"] == "not_configured"


def test_branch_lineage_not_configured_when_not_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(git_tools.gitctx, "_is_repo", lambda root: False)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    result = git_tools.dispatch("project_context.get_branch_lineage", {"base": "main"}, ctx)
    assert _payload(result)["status"] == "not_configured"


# ── ref validation / option-injection hardening ─────────────────────

_INJECTION = "--output=/tmp/ke-git-injection"


class _Recorder:
    """Stand-in for ``git_context.run_git``: records argv, answers like a repo."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, *, timeout=git_context.GIT_TIMEOUT_SECONDS):
        argv = list(args)
        self.calls.append(argv)
        stdout = {
            "rev-parse": "true",
            "merge-base": "a" * 40,
            "rev-list": "1\t2",
            "log": "subject line",
            "show": "3\t1\tsrc/a.py",
            "diff": "3\t1\tsrc/a.py",
        }.get(argv[0], "")
        return subprocess.CompletedProcess(["git", *argv], 0, stdout=stdout, stderr="")


def _diff_cfg() -> ProjectContextConfig:
    cfg = _enabled_cfg()
    return replace(cfg, git=replace(cfg.git, include_diff_summaries=True))


def _ref_calls(rec: _Recorder, ref: str) -> list[list[str]]:
    return [argv for argv in rec.calls if any(ref in arg for arg in argv)]


def _assert_ref_fenced(argv: list[str], ref: str) -> None:
    """The ref sits after ``--end-of-options`` and before any ``--``."""
    idx = next(i for i, arg in enumerate(argv) if ref in arg)
    assert "--end-of-options" in argv[:idx], argv
    if "--" in argv:
        assert argv.index("--") > idx, argv


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("project_context.search_by_diff", {"from_ref": _INJECTION}),
        ("project_context.search_by_diff", {"from_ref": "HEAD~1", "to_ref": _INJECTION}),
        ("project_context.get_branch_lineage", {"base": _INJECTION}),
        ("project_context.get_change_context", {"commit": _INJECTION}),
        ("project_context.get_change_context", {"commit": "HEAD@{1}"}),
        ("project_context.get_branch_lineage", {"base": "main..evil"}),
    ],
)
def test_unsafe_refs_rejected_before_git_runs(tmp_path: Path, monkeypatch, tool, args) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    ctx = _ctx(_diff_cfg(), tmp_path)
    payload = _payload(git_tools.dispatch(tool, args, ctx))
    assert payload["status"] == "invalid_args"
    assert "git ref" in payload["reason"]
    assert rec.calls == []


def test_search_by_diff_valid_refs_are_fenced(tmp_path: Path, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    ctx = _ctx(_diff_cfg(), tmp_path)
    payload = _payload(
        git_tools.dispatch(
            "project_context.search_by_diff", {"from_ref": "v1.2.3", "to_ref": "origin/main"}, ctx
        )
    )
    assert payload["files_changed"] == 1
    assert rec.calls == [
        ["diff", "--numstat", "--end-of-options", "v1.2.3", "origin/main", "--"],
    ]


def test_branch_lineage_valid_base_is_fenced(tmp_path: Path, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    payload = _payload(
        git_tools.dispatch("project_context.get_branch_lineage", {"base": "origin/main"}, ctx)
    )
    assert payload["base"] == "origin/main"
    assert (payload["behind"], payload["ahead"]) == (1, 2)
    calls = _ref_calls(rec, "origin/main")
    assert [argv[0] for argv in calls] == ["merge-base", "rev-list"]
    for argv in calls:
        _assert_ref_fenced(argv, "origin/main")


def test_change_context_valid_commit_is_fenced(tmp_path: Path, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    sha = "0123456789abcdef0123456789abcdef01234567"
    payload = _payload(
        git_tools.dispatch("project_context.get_change_context", {"commit": sha}, ctx)
    )
    assert payload["commit"] == sha
    assert payload["files_changed"] == 1
    calls = _ref_calls(rec, sha)
    assert [argv[0] for argv in calls] == ["log", "show"]
    for argv in calls:
        _assert_ref_fenced(argv, sha)
        assert argv[-1] == "--"


def test_explain_file_history_path_follows_double_dash(tmp_path: Path, monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    git_tools.dispatch(
        "project_context.explain_file_history", {"path": "--output=x", "limit": -5}, ctx
    )
    log_calls = [argv for argv in rec.calls if argv[0] == "log"]
    assert log_calls == [["log", "-1", "--format=%H\t%s", "--", "--output=x"]]


@pytest.mark.parametrize(
    "args",
    [{"path": "a\x00b"}, {"path": "ok.py", "limit": "many"}, {"path": ""}],
)
def test_explain_file_history_invalid_args(tmp_path: Path, monkeypatch, args) -> None:
    rec = _Recorder()
    monkeypatch.setattr(git_context, "run_git", rec)
    ctx = _ctx(_enabled_cfg(), tmp_path)
    payload = _payload(git_tools.dispatch("project_context.explain_file_history", args, ctx))
    assert payload["status"] == "invalid_args"
    assert rec.calls == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not installed")
def test_search_by_diff_injection_creates_no_file_real_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "one"]):
        subprocess.run(["git", *ident, *argv], cwd=repo, check=True, capture_output=True)
    target = tmp_path / "pwned.txt"
    ctx = _ctx(_diff_cfg(), repo)
    payload = _payload(
        git_tools.dispatch(
            "project_context.search_by_diff", {"from_ref": f"--output={target}"}, ctx
        )
    )
    assert payload["status"] == "invalid_args"
    assert not target.exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
