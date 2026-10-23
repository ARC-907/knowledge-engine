"""Optional git metadata collection for the project-context subsystem.

All git access in the subsystem goes through :func:`run_git` (re-exported
from :mod:`knowledge_engine.core.util.proc`; argv list,
``shell=False``, bounded timeout), and :func:`git_output` is its gracefully
degrading wrapper. Git is entirely optional: if the ``git`` binary is missing,
the directory is not a repository, or ``git.enabled`` is ``False`` in config,
every public function degrades gracefully — :func:`collect` returns ``None``
and :func:`diff_summary` returns an empty
:class:`~knowledge_engine.project_context.models.DiffSummary`.

Refs supplied by callers (for example MCP tool arguments) are untrusted. They
must pass :func:`validate_ref` before they reach an argv, and every invocation
that carries one places ``--end-of-options`` before the refs and ``--`` after
them, so a value such as ``--output=/some/path`` can never be parsed as an
option.

Privacy: the remote URL is never stored raw. Only a SHA-256 hash of a sanitized
``host + path`` (credentials stripped) is recorded, so a project can be matched
to its remote without leaking the URL or any embedded token.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.util.hashing import sha256_hex
from ..core.util.proc import GIT_TIMEOUT_SECONDS, run_git
from .models import DiffSummary, GitContext

logger = logging.getLogger(__name__)

#: Upper bound on an accepted ref's length; real refs are far shorter.
MAX_REF_LENGTH = 256

# Characters ``git check-ref-format`` forbids anywhere in a ref name (control
# characters and whitespace are rejected separately).
_REF_FORBIDDEN_CHARS = frozenset("~^:?*[\\")

# Trailing ancestry operators accepted after a ref name: ``~``, ``~3``, ``^``, ``^2``.
_REV_SUFFIX_RE = re.compile(r"(?:[~^][0-9]{0,9})*")


def git_output(
    cwd: Path | str,
    args: Sequence[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str | None:
    """Run ``git <args>`` in ``cwd`` and return stripped stdout, or ``None``.

    Returns ``None`` if the binary is missing, the command fails (non-zero
    exit), or it times out. Never raises for those common failure modes; git
    is an optional capability.
    """
    try:
        proc = run_git(args, Path(cwd), timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s unavailable in %s: %s", _argv_head(args), cwd, exc)
        return None
    if proc.returncode != 0:
        logger.debug("git %s exited %d in %s", _argv_head(args), proc.returncode, cwd)
        return None
    return proc.stdout.strip()


def _argv_head(args: Sequence[str]) -> str:
    """Return the git subcommand for log lines (never the untrusted arguments)."""
    return args[0] if args else ""


def validate_ref(ref: object) -> str:
    """Return ``ref`` unchanged if it is a safe git revision, else raise ``ValueError``.

    Accepts what ``git check-ref-format --allow-onelevel`` accepts (``main``,
    ``origin/main``, ``v1.2.3``, a full or abbreviated hex SHA, ``HEAD``),
    optionally followed by ancestry operators (``HEAD~3``, ``main^``,
    ``HEAD^2~1``). Everything is checked locally; no subprocess is started.

    Rejected: non-strings, empty values, anything starting with ``-`` (option
    injection), control characters or whitespace, ``..`` (including range
    syntax), ``@{`` (reflog syntax), a lone ``@``, backslashes and the other
    characters git forbids in ref names, empty or dot-leading path components,
    components ending in ``.lock``, a trailing ``.`` or ``/``, and values longer
    than :data:`MAX_REF_LENGTH`.
    """
    if not isinstance(ref, str):
        raise ValueError(f"git ref must be a string, got {type(ref).__name__}")
    shown = ref if len(ref) <= 80 else ref[:77] + "..."
    if not ref:
        raise ValueError("git ref must not be empty")
    if len(ref) > MAX_REF_LENGTH:
        raise ValueError(f"git ref {shown!r} is longer than {MAX_REF_LENGTH} characters")
    if ref.startswith("-"):
        raise ValueError(f"git ref {shown!r} must not start with '-'")
    if any(ch.isspace() or not ch.isprintable() for ch in ref):
        raise ValueError(f"git ref {shown!r} contains whitespace or control characters")

    # Split ``name~2^`` into the ref name and its ancestry suffix.
    cut = min((i for i in (ref.find("~"), ref.find("^")) if i != -1), default=len(ref))
    name, suffix = ref[:cut], ref[cut:]
    if suffix and not _REV_SUFFIX_RE.fullmatch(suffix):
        raise ValueError(f"git ref {shown!r} has an unsupported revision suffix")

    reason = _check_ref_format(name)
    if reason:
        raise ValueError(f"git ref {shown!r} is not a valid ref name: {reason}")
    return ref


def _check_ref_format(name: str) -> str | None:
    """Mirror ``git check-ref-format --allow-onelevel``; return why ``name`` fails."""
    if not name:
        return "empty ref name"
    if name == "@":
        return "a lone '@' is not allowed"
    if "@{" in name:
        return "'@{' (reflog syntax) is not allowed"
    if ".." in name:
        return "'..' is not allowed"
    bad = sorted({ch for ch in name if ch in _REF_FORBIDDEN_CHARS})
    if bad:
        return f"forbidden character(s) {''.join(bad)!r}"
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return "empty path component"
    if name.endswith("."):
        return "must not end with '.'"
    for component in name.split("/"):
        if component.startswith("."):
            return "path components must not start with '.'"
        if component.endswith(".lock"):
            return "path components must not end with '.lock'"
    return None


def _is_repo(root: Path) -> bool:
    """True if ``root`` is inside a git work tree."""
    out = git_output(root, ["rev-parse", "--is-inside-work-tree"])
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
    url = git_output(root, ["remote", "get-url", "origin"])
    if not url:
        return None
    sanitized = _sanitize_remote(url)
    if not sanitized:
        return None
    return sha256_hex(sanitized)


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

    branch = git_output(root_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit_hash = git_output(root_path, ["rev-parse", "HEAD"])

    include_dirty = True
    if cfg is not None:
        include_dirty = getattr(getattr(cfg, "git", None), "include_dirty_status", True)
    dirty = False
    if include_dirty:
        status = git_output(root_path, ["status", "--porcelain"])
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
    or the diff fails, an all-zero summary is returned.

    Raises:
        ValueError: ``a`` or ``b`` is not a safe ref (see :func:`validate_ref`).
            Validation happens before any subprocess is started.
    """
    from_ref = validate_ref(a)
    to_ref = validate_ref(b)
    out = git_output(
        Path(root),
        ["diff", "--numstat", "--end-of-options", from_ref, to_ref, "--"],
    )
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
        f"{files_changed} file(s) changed, {insertions} insertion(s)(+), {deletions} deletion(s)(-)"
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
