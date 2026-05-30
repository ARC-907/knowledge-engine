"""Tests for project_docs.registry."""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from knowledge_engine.project_docs import db, fingerprints, registry
from knowledge_engine.project_docs.models import GitContext


def _registry(tmp_path):
    """Build a registry DB with only the 001_ migrations applied."""
    path = tmp_path / "registry.db"
    conn = db.connect(str(path))
    db.apply_migrations(conn, only_prefixes=("001_",))
    return conn


@dataclass
class _GitCfg:
    enabled: bool = True
    default_branch: str = "main"


@dataclass
class _Cfg:
    git: _GitCfg


def _cfg(enabled: bool = True) -> _Cfg:
    return _Cfg(git=_GitCfg(enabled=enabled))


def _install_git_context(monkeypatch, collect):
    """Install a fake ``git_context`` module exposing ``collect``.

    ``registry._detect_branch`` resolves the dependency with
    ``from . import git_context``, which returns the ``git_context`` *attribute*
    on the already-imported parent package if one exists. Once any earlier test
    in the suite imports the real submodule, that attribute is set, and patching
    only ``sys.modules`` would be silently ignored (the source of an
    order-dependent failure). So patch the package attribute too.
    """
    import knowledge_engine.project_docs as pkg

    module = types.ModuleType("knowledge_engine.project_docs.git_context")
    module.collect = collect
    monkeypatch.setitem(
        sys.modules,
        "knowledge_engine.project_docs.git_context",
        module,
    )
    monkeypatch.setattr(pkg, "git_context", module, raising=False)


def test_register_then_list_projects_has_one(tmp_path):
    conn = _registry(tmp_path)
    proj = tmp_path / "my-project"
    proj.mkdir()

    result = registry.register_project(conn, str(proj), _cfg())

    assert result["name"] == "my-project"
    assert result["project_fp"].startswith("proj_")
    assert result["root_path"]

    projects = registry.list_projects(conn)
    assert len(projects) == 1
    assert projects[0]["project_fp"] == result["project_fp"]
    assert projects[0]["name"] == "my-project"


def test_register_project_explicit_name(tmp_path):
    conn = _registry(tmp_path)
    proj = tmp_path / "raw-dir"
    proj.mkdir()

    result = registry.register_project(
        conn, str(proj), _cfg(), name="Friendly Name"
    )
    assert result["name"] == "Friendly Name"


def test_register_project_is_idempotent(tmp_path):
    conn = _registry(tmp_path)
    proj = tmp_path / "dup"
    proj.mkdir()

    first = registry.register_project(conn, str(proj), _cfg())
    second = registry.register_project(conn, str(proj), _cfg())

    assert first["project_fp"] == second["project_fp"]
    assert len(registry.list_projects(conn)) == 1


def test_validate_project_unknown(tmp_path):
    conn = _registry(tmp_path)
    result = registry.validate_project(conn, "proj_doesnotexist")
    assert result == {"exists": False, "name": None, "branch_count": 0}


def test_validate_project_known(tmp_path):
    conn = _registry(tmp_path)
    proj = tmp_path / "validate-me"
    proj.mkdir()
    registered = registry.register_project(conn, str(proj), _cfg())
    fingerprints.ensure_branch(conn, registered["project_fp"], "main")

    result = registry.validate_project(conn, registered["project_fp"])
    assert result["exists"] is True
    assert result["name"] == "validate-me"
    assert result["branch_count"] == 1


def test_list_branches_scoped_to_project(tmp_path):
    conn = _registry(tmp_path)
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    fp_a = registry.register_project(conn, str(a), _cfg())["project_fp"]
    fp_b = registry.register_project(conn, str(b), _cfg())["project_fp"]
    fingerprints.ensure_branch(conn, fp_a, "main")
    fingerprints.ensure_branch(conn, fp_a, "dev")
    fingerprints.ensure_branch(conn, fp_b, "main")

    branches_a = registry.list_branches(conn, fp_a)
    assert {row["branch"] for row in branches_a} == {"main", "dev"}
    assert all(row["project_fp"] == fp_a for row in branches_a)

    branches_b = registry.list_branches(conn, fp_b)
    assert len(branches_b) == 1


def test_current_context_with_git_branch(tmp_path, monkeypatch):
    conn = _registry(tmp_path)
    proj = tmp_path / "ctx-git"
    proj.mkdir()

    def fake_collect(root, cfg):
        return GitContext(branch="feature/x", commit_hash="abc1234", dirty=False)

    _install_git_context(monkeypatch, fake_collect)

    ctx = registry.current_context(str(proj), _cfg(), conn)

    assert ctx["project_fp"].startswith("proj_")
    assert ctx["branch_fp"].startswith("br_")
    assert ctx["name"] == "ctx-git"
    assert ctx["branch"] == "feature/x"
    assert ctx["git_available"] is True

    # The branch was allocated in the registry.
    branches = registry.list_branches(conn, ctx["project_fp"])
    assert any(row["branch"] == "feature/x" for row in branches)


def test_current_context_git_collect_returns_none(tmp_path, monkeypatch):
    conn = _registry(tmp_path)
    proj = tmp_path / "ctx-none"
    proj.mkdir()

    def fake_collect(root, cfg):
        return None

    _install_git_context(monkeypatch, fake_collect)

    ctx = registry.current_context(str(proj), _cfg(), conn)

    assert ctx["project_fp"].startswith("proj_")
    assert ctx["branch_fp"].startswith("br_")
    assert ctx["branch"] == "main"
    assert ctx["git_available"] is False

    branches = registry.list_branches(conn, ctx["project_fp"])
    assert any(row["branch"] == "main" for row in branches)


def test_current_context_git_disabled(tmp_path, monkeypatch):
    conn = _registry(tmp_path)
    proj = tmp_path / "ctx-disabled"
    proj.mkdir()

    called = {"hit": False}

    def fake_collect(root, cfg):
        called["hit"] = True
        return GitContext(branch="feature/x", commit_hash=None, dirty=False)

    _install_git_context(monkeypatch, fake_collect)

    ctx = registry.current_context(str(proj), _cfg(enabled=False), conn)

    assert ctx["branch"] == "main"
    assert ctx["git_available"] is False
    assert called["hit"] is False


def test_current_context_no_git_context_module(tmp_path, monkeypatch):
    conn = _registry(tmp_path)
    proj = tmp_path / "ctx-absent"
    proj.mkdir()

    # Treat the optional module as absent. Patch both sys.modules and the
    # parent-package attribute (which `from . import git_context` resolves
    # first) so this holds regardless of test order.
    import knowledge_engine.project_docs as pkg

    monkeypatch.setitem(
        sys.modules,
        "knowledge_engine.project_docs.git_context",
        None,
    )
    monkeypatch.setattr(pkg, "git_context", None, raising=False)

    ctx = registry.current_context(str(proj), _cfg(), conn)
    assert ctx["branch"] == "main"
    assert ctx["git_available"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
