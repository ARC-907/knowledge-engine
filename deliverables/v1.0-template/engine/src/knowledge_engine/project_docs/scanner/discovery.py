"""Filesystem walking and detector orchestration for the project-docs scanner.

:func:`walk` yields the files under a project root that survive the scanner
discovery gates (always-ignored directories, ``.gitignore`` patterns, the symlink
policy, and the maximum file-size limit). :func:`run_detectors` fans a set of
detectors out, tolerating a single misbehaving detector without aborting the
whole scan.

The ``.gitignore`` handling here is deliberately shallow: simple line-prefix and
glob matching, not full gitignore semantics. It is enough to keep an ingest from
slurping obviously-ignored paths.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..models import Candidate

logger = logging.getLogger(__name__)

# Directories that are always skipped regardless of configuration.
ALWAYS_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


def _load_gitignore_patterns(root: Path) -> list[str]:
    """Return non-empty, non-comment patterns from ``root/.gitignore``."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []
    try:
        text = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    patterns: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def _matches_gitignore(rel_posix: str, name: str, patterns: Iterable[str]) -> bool:
    """Return True if a relative path or basename matches a gitignore pattern.

    Simplified matcher: leading/trailing slashes are stripped, then the pattern
    is compared against both the basename and the full relative (posix) path
    using glob semantics, and also treated as a directory/path prefix.
    """
    for raw in patterns:
        pat = raw.lstrip("/").rstrip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_posix, pat):
            return True
        # Directory-style prefix match (e.g. "secrets" ignores "secrets/x").
        if rel_posix == pat or rel_posix.startswith(pat + "/"):
            return True
    return False


def walk(root, cfg) -> Iterator[Path]:
    """Yield files under ``root`` that pass the scanner discovery gates.

    Honors ``cfg.scanner.respect_gitignore``, ``cfg.scanner.follow_symlinks``
    (default False), and ``cfg.scanner.max_file_bytes``. Always-ignored
    directories are pruned regardless of configuration.
    """
    root = Path(root)
    scanner = cfg.scanner
    respect_gitignore = scanner.respect_gitignore
    follow_symlinks = scanner.follow_symlinks
    max_file_bytes = scanner.max_file_bytes
    patterns = _load_gitignore_patterns(root) if respect_gitignore else []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        current = Path(dirpath)

        # Prune directories in place so os.walk does not descend into them.
        kept_dirs: list[str] = []
        for dirname in dirnames:
            if dirname in ALWAYS_SKIP_DIRS:
                continue
            child = current / dirname
            if not follow_symlinks and child.is_symlink():
                continue
            if respect_gitignore:
                rel = child.relative_to(root).as_posix()
                if _matches_gitignore(rel, dirname, patterns):
                    continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            file_path = current / filename
            if not follow_symlinks and file_path.is_symlink():
                continue
            if respect_gitignore:
                rel = file_path.relative_to(root).as_posix()
                if _matches_gitignore(rel, filename, patterns):
                    continue
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                continue
            yield file_path


def run_detectors(root, cfg, detectors) -> list[Candidate]:
    """Run each detector and return the combined list of candidates.

    A detector that raises is logged and skipped so that one faulty detector
    cannot abort the whole scan.
    """
    root = Path(root)
    candidates: list[Candidate] = []
    for detector in detectors:
        try:
            found = list(detector.discover(root, cfg))
        except Exception:  # noqa: BLE001 - isolate a single detector's failure
            logger.exception(
                "detector %s failed during discovery",
                getattr(detector, "name", detector),
            )
            continue
        candidates.extend(found)
    return candidates
