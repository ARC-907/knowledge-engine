"""Containment and decoding hardening for pointer apply / rollback.

Plan items and ledger rows live in the database, so their paths are untrusted.
These tests plant hostile rows directly (bypassing the planner) and check that
nothing outside the project root is ever written, that a non-UTF-8 source is a
per-item error, and that the remaining items still apply. Fully offline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_engine.project_context import db, fingerprints
from knowledge_engine.project_context.config import ProjectContextConfig
from knowledge_engine.project_context.scanner import pointer_apply
from knowledge_engine.project_context.scanner.pointer_plan import BACKUP_SUFFIX

PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")

SOURCE = '''def greet(name):
    """This is a long docstring.

    It spans several lines and is a good candidate for replacement.
    """
    return f"hello {name}"
'''
SPAN = [2, 5]


def _all_gates_on() -> ProjectContextConfig:
    base = ProjectContextConfig()
    pointer = replace(
        base.scanner.pointer_replacement,
        enabled=True,
        allow_source_mutation=True,
        write_backups=True,
    )
    scanner = replace(base.scanner, enabled=True, dry_run=False, pointer_replacement=pointer)
    return replace(base, scanner=scanner)


@pytest.fixture
def env(tmp_path: Path):
    """Project root nested two levels down, plus open project/registry DBs."""
    root = tmp_path / "outer" / "root"
    root.mkdir(parents=True)
    project = db.connect(":memory:")
    db.apply_migrations(project, only_prefixes=PROJECT_PREFIXES)
    registry = db.connect(":memory:")
    db.apply_migrations(registry, only_prefixes=("001_",))
    project_fp = fingerprints.ensure_project(registry, str(root), "sample")
    branch_fp = fingerprints.ensure_branch(registry, project_fp, "main")
    yield {
        "tmp": tmp_path,
        "root": root,
        "project": project,
        "registry": registry,
        "project_fp": project_fp,
        "branch_fp": branch_fp,
    }
    project.close()
    registry.close()


def _plan(project: sqlite3.Connection, *targets: str) -> str:
    plan_id = str(uuid.uuid4())
    items = [{"target_file": t, "span": SPAN} for t in targets]
    project.execute(
        "INSERT INTO pointer_rewrite_plans (plan_id, created_at, dry_run, items_json, status) "
        "VALUES (?, '2026-01-01T00:00:00+00:00', 1, ?, 'planned')",
        (plan_id, json.dumps(items)),
    )
    project.commit()
    return plan_id


def _apply(env: dict, plan_id: str) -> dict:
    return pointer_apply.run(
        plan_id,
        str(env["root"]),
        _all_gates_on(),
        env["project"],
        project_fp=env["project_fp"],
        branch_fp=env["branch_fp"],
        confirm=True,
        registry_conn=env["registry"],
    )


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks unavailable: {exc}")


def _error_events(project: sqlite3.Connection) -> list[str]:
    rows = project.execute(
        "SELECT result FROM pointer_rewrite_events WHERE action = 'error' ORDER BY id"
    ).fetchall()
    return [r["result"] for r in rows]


# ── apply: hostile targets are never written ────────────────────────


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink"])
def test_apply_refuses_targets_outside_root(env: dict, kind: str, caplog) -> None:
    outside = env["tmp"] / "evil.txt"
    outside.write_text(SOURCE, encoding="utf-8")
    before = outside.read_bytes()
    if kind == "traversal":
        target = "../../evil.txt"
    elif kind == "absolute":
        target = str(outside)
    else:
        _symlink(env["root"] / "link.py", outside)
        target = "link.py"

    with caplog.at_level("WARNING", logger=pointer_apply.__name__):
        result = _apply(env, _plan(env["project"], target))

    assert result["status"] == "applied"
    assert result["applied"] == []
    assert result["errors"] == [{"target_file": target, "reason": "path_outside_root"}]
    assert outside.read_bytes() == before
    assert not Path(str(outside) + BACKUP_SUFFIX).exists()
    assert _error_events(env["project"]) == ["path_outside_root"]
    assert any("outside root" in rec.getMessage() for rec in caplog.records)
    pointers_written = env["project"].execute("SELECT COUNT(*) AS n FROM doc_pointers").fetchone()
    assert pointers_written["n"] == 0


def test_apply_refuses_backup_symlink_escaping_root(env: dict) -> None:
    src = env["root"] / "mod.py"
    src.write_text(SOURCE, encoding="utf-8")
    victim = env["tmp"] / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    _symlink(env["root"] / ("mod.py" + BACKUP_SUFFIX), victim)

    result = _apply(env, _plan(env["project"], "mod.py"))

    assert result["errors"] == [{"target_file": "mod.py", "reason": "backup_path_outside_root"}]
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert src.read_text(encoding="utf-8") == SOURCE


def test_apply_ignores_planted_temp_file_symlink(env: dict) -> None:
    """The old fixed ``<name>.ke-tmp`` path must not be followed out of root."""
    src = env["root"] / "mod.py"
    src.write_text(SOURCE, encoding="utf-8")
    victim = env["tmp"] / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    _symlink(env["root"] / "mod.py.ke-tmp", victim)

    result = _apply(env, _plan(env["project"], "mod.py"))

    assert len(result["applied"]) == 1, result
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert '"""See ke-doc://docstring/' in src.read_text(encoding="utf-8")
    leftovers = [p.name for p in env["root"].iterdir() if p.name.endswith(".ke-tmp")]
    assert leftovers == ["mod.py.ke-tmp"]  # only the planted link remains


# ── apply: non-UTF-8 source is a per-item error ─────────────────────


def test_apply_latin1_source_is_item_error_and_others_continue(env: dict, caplog) -> None:
    latin = env["root"] / "latin.py"
    latin_bytes = SOURCE.replace("long docstring", "café docstring").encode("latin-1")
    latin.write_bytes(latin_bytes)
    good = env["root"] / "good.py"
    good.write_text(SOURCE, encoding="utf-8")

    with caplog.at_level("WARNING", logger=pointer_apply.__name__):
        result = _apply(env, _plan(env["project"], "latin.py", "good.py"))

    assert result["errors"] == [{"target_file": "latin.py", "reason": "source_not_utf8"}]
    assert [item["target_file"] for item in result["applied"]] == ["good.py"]
    assert latin.read_bytes() == latin_bytes
    assert not Path(str(latin) + BACKUP_SUFFIX).exists()
    assert '"""See ke-doc://docstring/' in good.read_text(encoding="utf-8")
    assert any("not valid UTF-8" in rec.getMessage() for rec in caplog.records)


def test_apply_preserves_file_mode(env: dict) -> None:
    src = env["root"] / "mod.py"
    src.write_text(SOURCE, encoding="utf-8")
    src.chmod(0o750)
    result = _apply(env, _plan(env["project"], "mod.py"))
    assert len(result["applied"]) == 1, result
    if os.name == "posix":
        assert src.stat().st_mode & 0o777 == 0o750


# ── rollback: containment re-checked ────────────────────────────────


def _apply_one(env: dict) -> tuple[str, Path, bytes]:
    src = env["root"] / "mod.py"
    src.write_text(SOURCE, encoding="utf-8")
    original = src.read_bytes()
    plan_id = _plan(env["project"], "mod.py")
    result = _apply(env, plan_id)
    assert len(result["applied"]) == 1, result
    return plan_id, src, original


def test_rollback_uses_recorded_root(env: dict) -> None:
    plan_id, src, original = _apply_one(env)
    detail = (
        env["project"]
        .execute("SELECT detail FROM pointer_rewrite_events WHERE action = 'apply'")
        .fetchone()["detail"]
    )
    assert json.loads(detail)["root"] == str(env["root"].resolve())

    rb = pointer_apply.rollback(plan_id, env["project"])
    assert rb["errors"] == []
    assert len(rb["restored"]) == 1
    assert src.read_bytes() == original


def test_rollback_refuses_tampered_target_outside_root(env: dict, caplog) -> None:
    plan_id, src, _ = _apply_one(env)
    victim = env["tmp"] / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    detail = {"target_file": str(victim), "root": str(env["root"].resolve())}
    env["project"].execute(
        "UPDATE pointer_rewrite_events SET detail = ? WHERE action = 'apply'",
        (json.dumps(detail),),
    )
    env["project"].commit()
    stubbed = src.read_bytes()

    with caplog.at_level("WARNING", logger=pointer_apply.__name__):
        rb = pointer_apply.rollback(plan_id, env["project"])

    assert rb["restored"] == []
    assert [e["reason"] for e in rb["errors"]] == ["path_outside_root"]
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert src.read_bytes() == stubbed
    assert "path_outside_root" in _error_events(env["project"])
    assert any("refusing to restore" in rec.getMessage() for rec in caplog.records)


def test_rollback_explicit_root_overrides_recorded_root(env: dict) -> None:
    plan_id, src, _ = _apply_one(env)
    other_root = env["tmp"] / "elsewhere"
    other_root.mkdir()
    rb = pointer_apply.rollback(plan_id, env["project"], root=other_root)
    assert [e["reason"] for e in rb["errors"]] == ["path_outside_root"]
    assert '"""See ke-doc://docstring/' in src.read_text(encoding="utf-8")


def _legacy_event(project: sqlite3.Connection, target: Path, backup: Path) -> str:
    """Insert an apply event shaped like those written before roots were recorded."""
    plan_id = _plan(project)
    project.execute(
        "INSERT INTO pointer_rewrite_events "
        "(plan_id, pointer_id, ts, action, backup_path, result, detail) "
        "VALUES (?, NULL, '2026-01-01T00:00:00+00:00', 'apply', ?, 'ok', ?)",
        (plan_id, str(backup), json.dumps({"target_file": str(target)})),
    )
    project.commit()
    return plan_id


def test_rollback_legacy_event_without_root_restores_paired_backup(env: dict) -> None:
    target = env["root"] / "old.py"
    target.write_text("stub\n", encoding="utf-8")
    backup = Path(str(target) + BACKUP_SUFFIX)
    backup.write_text(SOURCE, encoding="utf-8")
    plan_id = _legacy_event(env["project"], target, backup)

    rb = pointer_apply.rollback(plan_id, env["project"])

    assert rb["errors"] == []
    assert len(rb["restored"]) == 1
    assert target.read_text(encoding="utf-8") == SOURCE


def test_rollback_legacy_event_rejects_unpaired_target(env: dict) -> None:
    backup = env["root"] / ("old.py" + BACKUP_SUFFIX)
    backup.write_text(SOURCE, encoding="utf-8")
    victim = env["tmp"] / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    plan_id = _legacy_event(env["project"], victim, backup)

    rb = pointer_apply.rollback(plan_id, env["project"])

    assert [e["reason"] for e in rb["errors"]] == ["target_backup_mismatch"]
    assert victim.read_text(encoding="utf-8") == "keep me\n"


# ── _ensure_within unit checks ───────────────────────────────────────


def test_ensure_within(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    assert pointer_apply._ensure_within(root, "sub/x.py") == (root / "sub" / "x.py").resolve()
    assert pointer_apply._ensure_within(root, root / "a.py") == (root / "a.py").resolve()
    for bad in ("../x.py", "sub/../../x.py", str(tmp_path / "x.py"), "/etc/passwd"):
        with pytest.raises(ValueError, match="outside the project root"):
            pointer_apply._ensure_within(root, bad)
    with pytest.raises(ValueError, match="cannot resolve"):
        pointer_apply._ensure_within(root, "nul\x00byte")
