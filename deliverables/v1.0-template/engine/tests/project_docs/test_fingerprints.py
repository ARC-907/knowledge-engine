"""Tests for project/branch fingerprint allocation, collision, and validation."""

from __future__ import annotations

import sqlite3

import pytest

from knowledge_engine.project_docs import db
from knowledge_engine.project_docs.fingerprints import (
    ContextError,
    branch_fp,
    ensure_branch,
    ensure_project,
    project_fp,
    validate_context,
)


@pytest.fixture()
def registry_conn(tmp_path) -> sqlite3.Connection:
    """A fresh registry DB with only the 001_ migration applied."""
    conn = db.connect(tmp_path / "registry.sqlite")
    db.apply_migrations(conn, only_prefixes=("001_",))
    return conn


def test_project_fp_is_deterministic() -> None:
    a = project_fp("/home/user/proj")
    b = project_fp("/home/user/proj")
    assert a == b
    assert a.startswith("proj_")
    assert len(a) == len("proj_") + 16


def test_project_fp_remote_identity_changes_value() -> None:
    plain = project_fp("/home/user/proj")
    salted = project_fp("/home/user/proj", remote_identity="github.com/acme/proj")
    assert plain != salted


def test_branch_fp_is_deterministic_and_scoped() -> None:
    pfp = project_fp("/home/user/proj")
    a = branch_fp(pfp, "main")
    b = branch_fp(pfp, "main")
    assert a == b
    assert a.startswith("br_")
    assert a != branch_fp(pfp, "dev")


def test_ensure_project_idempotent(registry_conn: sqlite3.Connection) -> None:
    root = "/home/user/proj"
    fp1 = ensure_project(registry_conn, root, "proj")
    fp2 = ensure_project(registry_conn, root, "proj")
    assert fp1 == fp2

    count = registry_conn.execute(
        "SELECT COUNT(*) AS c FROM projects WHERE project_fp = ?", (fp1,)
    ).fetchone()["c"]
    assert count == 1


def test_ensure_project_collision_raises(registry_conn: sqlite3.Connection) -> None:
    fp = ensure_project(registry_conn, "/home/user/proj", "proj")
    # Force a record with a colliding fingerprint but a different stored root.
    registry_conn.execute(
        "UPDATE projects SET root_path = ? WHERE project_fp = ?",
        ("/home/user/OTHER", fp),
    )
    registry_conn.commit()

    with pytest.raises(ContextError):
        ensure_project(registry_conn, "/home/user/proj", "proj")


def test_ensure_project_collision_override_reassigns(
    registry_conn: sqlite3.Connection,
) -> None:
    fp = ensure_project(registry_conn, "/home/user/proj", "proj")
    registry_conn.execute(
        "UPDATE projects SET root_path = ? WHERE project_fp = ?",
        ("/home/user/OTHER", fp),
    )
    registry_conn.commit()

    fp2 = ensure_project(registry_conn, "/home/user/proj", "proj", override=fp)
    assert fp2 == fp
    stored = registry_conn.execute(
        "SELECT root_path FROM projects WHERE project_fp = ?", (fp,)
    ).fetchone()["root_path"]
    assert stored == "/home/user/proj"


def test_ensure_branch_retroactive_allocation(registry_conn: sqlite3.Connection) -> None:
    pfp = ensure_project(registry_conn, "/home/user/proj", "proj")
    bfp = ensure_branch(registry_conn, pfp, "feature/x")
    assert bfp.startswith("br_")

    row = registry_conn.execute(
        "SELECT project_fp, branch_name FROM branches WHERE branch_fp = ?", (bfp,)
    ).fetchone()
    assert row["project_fp"] == pfp
    assert row["branch_name"] == "feature/x"

    # Idempotent on repeat.
    assert ensure_branch(registry_conn, pfp, "feature/x") == bfp
    count = registry_conn.execute(
        "SELECT COUNT(*) AS c FROM branches WHERE branch_fp = ?", (bfp,)
    ).fetchone()["c"]
    assert count == 1


def test_validate_context_passes_after_ensure(registry_conn: sqlite3.Connection) -> None:
    pfp = ensure_project(registry_conn, "/home/user/proj", "proj")
    bfp = ensure_branch(registry_conn, pfp, "main")
    # Should not raise.
    validate_context(registry_conn, pfp, bfp)


def test_validate_context_raises_before_ensure(
    registry_conn: sqlite3.Connection,
) -> None:
    pfp = project_fp("/home/user/proj")
    bfp = branch_fp(pfp, "main")
    with pytest.raises(ContextError):
        validate_context(registry_conn, pfp, bfp)


def test_validate_context_raises_for_unknown_branch(
    registry_conn: sqlite3.Connection,
) -> None:
    pfp = ensure_project(registry_conn, "/home/user/proj", "proj")
    bfp = branch_fp(pfp, "never-allocated")
    with pytest.raises(ContextError):
        validate_context(registry_conn, pfp, bfp)


def test_fingerprint_events_logged(registry_conn: sqlite3.Connection) -> None:
    pfp = ensure_project(registry_conn, "/home/user/proj", "proj")
    bfp = ensure_branch(registry_conn, pfp, "main")
    validate_context(registry_conn, pfp, bfp)

    kinds = {
        r["kind"]
        for r in registry_conn.execute("SELECT kind FROM fingerprint_events").fetchall()
    }
    assert "project_allocated" in kinds
    assert "branch_allocated" in kinds
    assert "validate_ok" in kinds
