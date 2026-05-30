"""Path resolution for project-docs storage.

All paths are derived from a *project root* (the user's repository) plus the
configured relative locations. Nothing here writes files; callers do.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import ProjectDocsConfig


def slugify(name: str) -> str:
    """Lowercase, alnum + ``-`` slug. Mirrors ``knowledge_engine.cli._slugify``."""
    out: list[str] = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "&", "."):
            out.append("-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-") or "project"


def _git_toplevel(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if top else None


def resolve_project_root(start: Path | None = None, cfg: ProjectDocsConfig | None = None) -> Path:
    """Resolve the project root.

    Preference order: git top-level of ``start`` → ``start`` itself. ``cfg`` is
    accepted for symmetry / future override hooks but not required.
    """
    base = (start or Path.cwd()).resolve()
    top = _git_toplevel(base)
    if top is not None:
        return top
    return base


def canonical_root(root: Path) -> str:
    """A normalized, comparable string form of a project root.

    Case-folded on Windows (where the filesystem is case-insensitive) so the
    same project always derives the same fingerprint regardless of how the path
    was typed. POSIX paths are left case-sensitive.
    """
    resolved = Path(root).resolve().as_posix()
    if sys.platform == "win32":
        return resolved.casefold()
    return resolved


def project_docs_dir(root: Path, cfg: ProjectDocsConfig) -> Path:
    """Directory holding per-project content DBs."""
    return (Path(root) / cfg.database_dir).resolve()


def project_db_path(root: Path, cfg: ProjectDocsConfig, slug: str) -> Path:
    """Path to a single project's content DB."""
    return project_docs_dir(root, cfg) / f"{slugify(slug)}.sqlite"


def fingerprint_db_path(root: Path, cfg: ProjectDocsConfig) -> Path:
    """Path to the shared fingerprint registry DB."""
    return (Path(root) / cfg.fingerprint_database).resolve()
