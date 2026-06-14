"""Optional git metadata collection for the project-docs subsystem.

All git access goes through ``subprocess.run`` with an argv list and
``shell=False`` (matching the repo's ``host.py`` security posture). Git is
entirely optional: if the ``git`` binary is missing, the directory is not a
repository, or ``git.enabled`` is ``False`` in config, every public function
degrades gracefully — :func:`collect` returns ``None`` and :func:`diff_summary`
returns an empty :class:`~knowledge_engine.project_docs.models.DiffSummary`.

Privacy: the remote URL is never stored raw. Only a SHA-256 hash of a sanitized
``host + path`` (credentials stripped) is recorded, so a project can be matched
to its remote without leaking the URL or any embedded token.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .models import DiffSummary, GitContext

# Conservative timeout so a hung/locked repo can never stall ingestion.
_GIT_TIMEOUT = 10


def _run_git(root: Path, args: list[str]) -> str | None:
    """Run ``git <args>`` in ``root`` and return stripped stdout.

    Returns ``None`` if the binary is missing, the command fails (non-zero
    exit), or it times out. Never raises for the common failure modes; this is
    an optional capability.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            shell=False,
            timeout=_GIT_TIMEOUT,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _is_repo(root: Path) -> bool:
    """True if ``root`` is inside a git work tree."""
    out = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return out == "true"


def _sanitize_remote(url: str) -> str:
    """Reduce a git remote URL to ``host/path`` with all credentials stripped.

    Handles both ``https://user:pass@host/path.git`` and the ``scp``-like
    ``git@host:path.git`` form. The result is a stable, credential-free string
    suitable for hashing — it is never stored or returned raw.
    """
    text = url.strip()
    # Strip a leading scheme (https://, ssh://, git://, ...).
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    # Strip userinfo (user[:pass]@) — this removes embedded credentials.
    text = re.sub(r"^[^@/]*@", "", text)
    # Normalize the scp-like "host:path" separator to "host/path".
    text = text.replace(":", "/", 1)
    # Drop a trailing ".git" and any surrounding slashes for stability.
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text.strip("/")


def _remote_hash(root: Path) -> str | None:
    """SHA-256 of the sanitized origin remote, or ``None`` if no remote."""
    url = _run_git(root, ["remote", "get-url", "origin"])
    if not url:
        return None
    sanitized = _sanitize_remote(url)
    if not sanitized:
        return None
    return hashlib.sha256(sanitized.encode("utf-8")).hexdigest()


def collect(root: Path | str, cfg: Any) -> GitContext | None:
    """Collect optional git context for ``root``.

    Returns ``None`` when git is disabled in config, the ``git`` binary is
    missing, or ``root`` is not a git repository. ``dirty`` is only populated
    when ``cfg.git.include_dirty_status`` is true (otherwise reported as
    ``False``). The sanitized remote hash is included when a remote exists.
    """
    if cfg is not None and not getattr(getattr(cfg, "git", None), "enabled", True):
        return None

    root_path = Path(root)
    if not _is_repo(root_path):
        return None

    branch = _run_git(root_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit_hash = _run_git(root_path, ["rev-parse", "HEAD"])

    include_dirty = True
    if cfg is not None:
        include_dirty = getattr(getattr(cfg, "git", None), "include_dirty_status", True)
    dirty = False
    if include_dirty:
        status = _run_git(root_path, ["status", "--porcelain"])
        dirty = bool(status)

    remote_hash = _remote_hash(root_path)

    return GitContext(
        branch=branch,
        commit_hash=commit_hash,
        dirty=dirty,
        remote_hash=remote_hash,
        data=None,
    )


def _parse_numstat(text: str) -> tuple[int, int, int]:
    """Parse ``git diff --numstat`` output.

    Each line is ``<insertions>\\t<deletions>\\t<path>``. Binary files report
    ``-`` for both counts and contribute to ``files_changed`` but not to the
    line totals. Returns ``(files_changed, insertions, deletions)``.
    """
    files_changed = 0
    insertions = 0
    deletions = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files_changed += 1
        add_str, del_str = parts[0], parts[1]
        if add_str.isdigit():
            insertions += int(add_str)
        if del_str.isdigit():
            deletions += int(del_str)
    return files_changed, insertions, deletions


def diff_summary(root: Path | str, a: str, b: str) -> DiffSummary:
    """Summarize ``git diff --numstat a b`` between two refs.

    Returns counts of files changed, insertions, and deletions plus a short
    human-readable summary line. Full diff text is intentionally out of scope
    here (gated behind ``git.include_full_diffs`` elsewhere). If git is missing
    or the diff fails, an all-zero summary is returned — never an exception.
    """
    out = _run_git(Path(root), ["diff", "--numstat", a, b])
    if out is None:
        return DiffSummary(
            from_ref=a,
            to_ref=b,
            files_changed=0,
            insertions=0,
            deletions=0,
            summary="git diff unavailable",
        )
    files_changed, insertions, deletions = _parse_numstat(out)
    summary = (
        f"{files_changed} file(s) changed, "
        f"{insertions} insertion(s)(+), {deletions} deletion(s)(-)"
    )
    return DiffSummary(
        from_ref=a,
        to_ref=b,
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
        summary=summary,
    )


def store_git_context(
    conn: sqlite3.Connection,
    project_fp: str,
    branch_fp: str,
    gctx: GitContext,
) -> None:
    """Insert a :class:`GitContext` row into the project DB ``git_context`` table.

    ``dirty`` is stored as ``0``/``1`` and ``data`` is serialized to a JSON
    string (defaulting to ``{}``) to match the migration-006 column shape.
    """
    data_json = json.dumps(gctx.data) if gctx.data is not None else "{}"
    conn.execute(
        "INSERT INTO git_context "
        "(project_fp, branch_fp, captured_at, branch, commit_hash, dirty, "
        " remote_hash, data_json) "
        "VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?)",
        (
            project_fp,
            branch_fp,
            gctx.branch,
            gctx.commit_hash,
            1 if gctx.dirty else 0,
            gctx.remote_hash,
            data_json,
        ),
    )
    conn.commit()
