"""Path resolution for project-context storage.

All paths are derived from a *project root* (the user's repository) plus the
configured relative locations. Nothing here writes files; callers do.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..core.util.proc import run_git
from ..core.util.slug import slugify as _slugify
from .config import ProjectContextConfig

#: Separators of the project-slug rule (the registry rule plus ``.``).
PROJECT_SLUG_SEPARATORS = " -_&."


def project_slug(name: str) -> str:
    """Lowercase, alnum + ``-`` slug of a project name; ``project`` when empty.

    The registry's folder-slug rule (:func:`knowledge_engine.core.util.slug.slugify`)
    with ``.`` also treated as a separator.
    """
    return _slugify(name, separators=PROJECT_SLUG_SEPARATORS, fallback="project")


#: Historical name of :func:`project_slug` (kept for callers and tests).
slugify = project_slug


def _git_toplevel(start: Path) -> Path | None:
    try:
        result = run_git(["rev-parse", "--show-toplevel"], start, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if top else None


def resolve_project_root(
    start: Path | None = None, cfg: ProjectContextConfig | None = None
) -> Path:
    """Resolve the project root.

    Preference order: git top-level of ``start`` → ``start`` itself. ``cfg`` is
    accepted for symmetry / future override hooks but not required.
    """
    base = (start or Path.cwd()).resolve()
    top = _git_toplevel(base)
    if top is not None:
        return top
    return base


def canonical_root(root: Path | str) -> str:
    """A normalized, comparable string form of a project root.

    Case-folded on Windows (where the filesystem is case-insensitive) so the
    same project always derives the same fingerprint regardless of how the path
    was typed. POSIX paths are left case-sensitive.
    """
    resolved = Path(root).resolve().as_posix()
    if sys.platform == "win32":
        return resolved.casefold()
    return resolved


def project_context_dir(root: Path, cfg: ProjectContextConfig) -> Path:
    """Directory holding per-project content DBs."""
    return (Path(root) / cfg.database_dir).resolve()


def project_db_path(root: Path, cfg: ProjectContextConfig, slug: str) -> Path:
    """Path to a single project's content DB."""
    return project_context_dir(root, cfg) / f"{slugify(slug)}.sqlite"


def fingerprint_db_path(root: Path, cfg: ProjectContextConfig) -> Path:
    """Path to the shared fingerprint registry DB."""
    return (Path(root) / cfg.fingerprint_database).resolve()
