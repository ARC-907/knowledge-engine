"""Project and branch fingerprint allocation, validation, and collision handling.

Fingerprints are *semi-deterministic*: they are derived by hashing canonical
inputs (so the same project/branch yields the same fingerprint across scans
without external state), but they are also *recorded* in the shared registry DB
which is the authoritative source for collision detection and manual override.

This module operates exclusively on a **registry** connection (a DB to which
``db.apply_registry_migrations(conn)`` has been applied). Every
allocation, override, collision, and validation is written to the
``fingerprint_events`` audit table.
"""

from __future__ import annotations

import base64
import json
import sqlite3

from ..core.util.hashing import sha256_digest, sha256_hex
from ..core.util.time import utc_now_iso

PROJECT_FP_PREFIX = "proj_"
BRANCH_FP_PREFIX = "br_"
_FP_LEN = 16


class ContextError(Exception):
    """Raised when a fingerprint collision or a missing context is detected."""


def _b32lower(digest: bytes) -> str:
    """Return a lowercase, unpadded base32 encoding of ``digest``."""
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def project_fp(canonical_root: str, remote_identity: str | None = None) -> str:
    """Derive a deterministic project fingerprint from canonical inputs.

    The fingerprint is ``proj_`` followed by the first 16 lowercase base32
    characters of the SHA-256 digest of the canonical root (optionally salted
    with a sanitized remote identity).
    """
    payload = (
        canonical_root if remote_identity is None else f"{canonical_root}\x00{remote_identity}"
    )
    digest = sha256_digest(payload)
    return PROJECT_FP_PREFIX + _b32lower(digest)[:_FP_LEN]


def branch_fp(project_fp: str, branch_name: str) -> str:
    """Derive a deterministic branch fingerprint from a project fp + branch name."""
    payload = f"{project_fp}:{branch_name}"
    digest = sha256_digest(payload)
    return BRANCH_FP_PREFIX + _b32lower(digest)[:_FP_LEN]


def _log_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    project_fp: str | None = None,
    branch_fp: str | None = None,
    detail: str = "",
    data: dict | None = None,
) -> None:
    """Append a row to ``fingerprint_events`` for auditability."""
    conn.execute(
        "INSERT INTO fingerprint_events (ts, kind, project_fp, branch_fp, detail, data_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), kind, project_fp, branch_fp, detail, json.dumps(data) if data else None),
    )


def ensure_project(
    conn: sqlite3.Connection,
    canonical_root: str,
    name: str,
    remote_identity: str | None = None,
    override: str | None = None,
) -> str:
    """Ensure a project is registered and return its fingerprint.

    Derives the fingerprint from ``canonical_root`` (+ ``remote_identity``),
    then reconciles against the registry:

    * If no project with that fingerprint exists, it is inserted into
      ``projects`` and ``project_fingerprints``.
    * If a project with that fingerprint exists with the *same* recorded
      ``root_path``, the call is idempotent (no duplicate row; same fp returned).
    * If a project with that fingerprint exists with a *different* recorded
      ``root_path``, this is a collision: ``ContextError`` is raised unless
      ``override`` is truthy, in which case the stored root is updated and the
      override is audited.

    Every allocation, idempotent hit, override, and collision is logged to
    ``fingerprint_events``.
    """
    fp = override or project_fp(canonical_root, remote_identity)
    remote_hash = sha256_hex(remote_identity) if remote_identity is not None else None

    row = conn.execute("SELECT root_path FROM projects WHERE project_fp = ?", (fp,)).fetchone()
    if row is None:
        _allocate_project(conn, fp, canonical_root, name, remote_hash, override)
    elif row["root_path"] == canonical_root:
        _log_event(
            conn,
            "project_validate",
            project_fp=fp,
            detail="idempotent",
            data={"root_path": canonical_root},
        )
    elif override:
        _reassign_project(conn, fp, row["root_path"], canonical_root, name, remote_hash)
    else:
        _record_collision(conn, fp, row["root_path"], canonical_root)
    conn.commit()
    return fp


def _allocate_project(
    conn: sqlite3.Connection,
    fp: str,
    canonical_root: str,
    name: str,
    remote_hash: str | None,
    override: str | None,
) -> None:
    """Insert a new ``projects`` + ``project_fingerprints`` row (uncommitted)."""
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO projects "
        "(project_fp, name, root_path, remote_identity_hash, created_at, updated_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fp, name, canonical_root, remote_hash, now, now, None),
    )
    conn.execute(
        "INSERT INTO project_fingerprints "
        "(project_fp, strategy, source_inputs_hash, manual_override, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            fp,
            "manual" if override else "derived",
            sha256_hex(canonical_root),
            1 if override else 0,
            now,
        ),
    )
    _log_event(
        conn,
        "project_allocated",
        project_fp=fp,
        detail=f"name={name}",
        data={"root_path": canonical_root, "override": bool(override)},
    )


def _record_collision(
    conn: sqlite3.Connection, fp: str, stored_root: str, canonical_root: str
) -> None:
    """Same derived fp, different recorded root, no override: audit and raise."""
    _log_event(
        conn,
        "project_collision",
        project_fp=fp,
        detail="root mismatch",
        data={"stored_root": stored_root, "incoming_root": canonical_root},
    )
    conn.commit()
    raise ContextError(
        f"fingerprint {fp} already maps to a different project root "
        f"({stored_root!r} != {canonical_root!r}); pass override to reassign"
    )


def _reassign_project(
    conn: sqlite3.Connection,
    fp: str,
    stored_root: str,
    canonical_root: str,
    name: str,
    remote_hash: str | None,
) -> None:
    """Override a collision: move the fingerprint to the new root (uncommitted)."""
    conn.execute(
        "UPDATE projects SET root_path = ?, name = ?, remote_identity_hash = ?, updated_at = ? "
        "WHERE project_fp = ?",
        (canonical_root, name, remote_hash, utc_now_iso(), fp),
    )
    conn.execute("UPDATE project_fingerprints SET manual_override = 1 WHERE project_fp = ?", (fp,))
    _log_event(
        conn,
        "project_override",
        project_fp=fp,
        detail="root reassigned via override",
        data={"stored_root": stored_root, "incoming_root": canonical_root},
    )


def ensure_branch(
    conn: sqlite3.Connection,
    project_fp: str,
    branch_name: str,
    override: str | None = None,
) -> str:
    """Ensure a branch is registered for ``project_fp`` and return its fingerprint.

    Branches are allocated on demand and may be allocated retroactively when a
    record references an as-yet-unknown branch. Idempotent on repeat calls.
    """
    fp = override or branch_fp(project_fp, branch_name)
    now = utc_now_iso()

    row = conn.execute("SELECT branch_fp FROM branches WHERE branch_fp = ?", (fp,)).fetchone()
    if row is not None:
        _log_event(
            conn,
            "branch_validate",
            project_fp=project_fp,
            branch_fp=fp,
            detail="idempotent",
            data={"branch_name": branch_name},
        )
        conn.commit()
        return fp

    conn.execute(
        "INSERT INTO branches (branch_fp, project_fp, branch_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (fp, project_fp, branch_name, now, now),
    )
    conn.execute(
        "INSERT INTO branch_fingerprints "
        "(branch_fp, project_fp, strategy, manual_override, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (fp, project_fp, "manual" if override else "derived", 1 if override else 0, now),
    )
    _log_event(
        conn,
        "branch_override" if override else "branch_allocated",
        project_fp=project_fp,
        branch_fp=fp,
        detail=f"branch_name={branch_name}",
        data={"override": bool(override)},
    )
    conn.commit()
    return fp


def validate_context(conn: sqlite3.Connection, project_fp: str, branch_fp: str) -> None:
    """Validate that both fingerprints exist in the registry.

    Raises :class:`ContextError` if the project or branch fingerprint is not
    recorded. Logs the validation outcome to ``fingerprint_events``.
    """
    project_row = conn.execute(
        "SELECT 1 FROM projects WHERE project_fp = ?", (project_fp,)
    ).fetchone()
    if project_row is None:
        _log_event(
            conn,
            "validate_failed",
            project_fp=project_fp,
            branch_fp=branch_fp,
            detail="unknown project_fp",
        )
        conn.commit()
        raise ContextError(f"unknown project fingerprint: {project_fp}")

    branch_row = conn.execute(
        "SELECT 1 FROM branches WHERE branch_fp = ? AND project_fp = ?",
        (branch_fp, project_fp),
    ).fetchone()
    if branch_row is None:
        _log_event(
            conn,
            "validate_failed",
            project_fp=project_fp,
            branch_fp=branch_fp,
            detail="unknown branch_fp",
        )
        conn.commit()
        raise ContextError(f"unknown branch fingerprint: {branch_fp}")

    _log_event(
        conn,
        "validate_ok",
        project_fp=project_fp,
        branch_fp=branch_fp,
        detail="context valid",
    )
    conn.commit()
