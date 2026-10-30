"""Repository state and contained file access for code evidence.

A code locator names the exact state of the code it points into (ADR 0007):

* a clean working tree is addressed by its commit (``git rev-parse HEAD``);
* a dirty one (any change ``git status`` reports, untracked files included,
  except Serena's own ``.serena/`` state, which it writes into every project
  it opens) is addressed by a **snapshot digest**, never by the commit -- uncommitted
  content is never presented as the commit's. The digest is
  :func:`~knowledge_engine.knowledge.evidence.extract.snapshot_digest`'s
  format over the changed paths (``{path: sha256 of the bytes}``, deleted
  paths as empty bytes) plus the ``HEAD`` commit, so the same dirty state
  always gives the same digest and any change to a dirty file changes it;
* a root that is not a git work tree has no usable version: its locators are
  ``unsupported``.

Every path Serena returns is resolved against the project root and must stay
inside it (symlinks followed); anything else is rejected.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..core.paths import ensure_within
from ..core.util.proc import run_git
from ..knowledge.contracts import canonical_json, sha256_digest

_logger = logging.getLogger(__name__)

#: Directories Serena itself writes into the project root (project file, caches,
#: memories). They are tool state, not code: changes there never make a tree dirty.
IGNORED_STATE_DIRS: tuple[str, ...] = (".serena",)
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
#: Seconds a computed repository state is reused (one search touches many hits).
STATE_TTL_S = 2.0
#: Largest file served or hashed for a snapshot (bytes).
MAX_FILE_BYTES = 16 * 1024 * 1024
#: Most changed paths hashed into one snapshot digest.
MAX_DIRTY_PATHS = 5000
_CHUNK = 1 << 20


class PathEscapeError(ValueError):
    """A path from Serena resolves outside the project root."""


@dataclass(frozen=True)
class RepoState:
    """The version a locator pins: exactly one of ``commit`` / ``snapshot_digest``, or neither."""

    commit: str | None
    snapshot_digest: str | None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.commit is not None or self.snapshot_digest is not None

    @property
    def version(self) -> str | None:
        """What a revision records as its upstream version."""
        return self.commit or self.snapshot_digest


def relative_path(root: Path, path: str) -> str:
    """``path`` as a repository-relative POSIX path inside ``root`` (or :class:`PathEscapeError`)."""
    if not isinstance(path, str) or not path or "\x00" in path:
        raise PathEscapeError(f"invalid path {path!r}")
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PathEscapeError(f"path {path!r} is not repository-relative")
    try:
        resolved = ensure_within(root, Path(*candidate.parts), label="the project root")
    except ValueError as exc:
        raise PathEscapeError(str(exc)) from exc
    rel = resolved.relative_to(Path(root).resolve()).as_posix()
    if not rel or rel == ".":
        raise PathEscapeError(f"path {path!r} names the project root")
    return rel


def read_file(root: Path, rel: str) -> bytes | None:
    """The bytes of ``rel`` under ``root`` (contained), or ``None`` when absent or too large."""
    target = Path(root).resolve() / Path(*PurePosixPath(relative_path(root, rel)).parts)
    try:
        if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            return None
        return target.read_bytes()
    except OSError as exc:
        _logger.warning("cannot read %s: %s", rel, exc)
        return None


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
    except (IsADirectoryError, FileNotFoundError):
        return sha256_digest(b"")
    return "sha256:" + digest.hexdigest()


def _dirty_paths(porcelain: str) -> list[str]:
    """Paths from ``git status --porcelain=v1 -z`` (both sides of a rename)."""
    paths: list[str] = []
    fields = porcelain.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in "RC" and index < len(fields):
            paths.append(fields[index])
            index += 1
    return sorted(set(paths))


def compute_state(root: Path) -> RepoState:
    """The current :class:`RepoState` of ``root`` (git calls are bounded by a timeout)."""
    try:
        head = run_git(["rev-parse", "--verify", "HEAD"], root)
        if head.returncode != 0:
            return RepoState(None, None, "not a git work tree with a commit")
        commit = head.stdout.strip()
        if not _COMMIT_RE.fullmatch(commit):
            return RepoState(None, None, "git reported an unexpected HEAD")
        status = run_git(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules",
                "--",
                ".",
                *(f":(exclude){name}" for name in IGNORED_STATE_DIRS),
            ],
            root,
        )
        if status.returncode != 0:
            return RepoState(None, None, "git status failed")
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.warning("git state of %s is unavailable: %s", root, exc)
        return RepoState(None, None, "git is not available")
    dirty = _dirty_paths(status.stdout)
    if not dirty:
        return RepoState(commit, None)
    if len(dirty) > MAX_DIRTY_PATHS:
        return RepoState(None, None, f"more than {MAX_DIRTY_PATHS} changed paths")
    resolved_root = Path(root).resolve()
    listing: dict[str, str] = {"\x00HEAD": sha256_digest(commit.encode("ascii"))}
    for rel in dirty:
        listing[rel] = _file_digest(resolved_root / Path(*PurePosixPath(rel).parts))
    digest = "sha256:" + hashlib.sha256(canonical_json(listing).encode("utf-8")).hexdigest()
    return RepoState(None, digest)


class RepoStateCache:
    """:func:`compute_state` per root, reused for :data:`STATE_TTL_S` seconds."""

    def __init__(self, ttl_s: float = STATE_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._states: dict[Path, tuple[float, RepoState]] = {}

    def get(self, root: Path) -> RepoState:
        now = time.monotonic()
        with self._lock:
            cached = self._states.get(root)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
        state = compute_state(root)
        with self._lock:
            self._states[root] = (time.monotonic(), state)
        return state

    def clear(self) -> None:
        with self._lock:
            self._states.clear()


__all__ = [
    "IGNORED_STATE_DIRS",
    "MAX_FILE_BYTES",
    "PathEscapeError",
    "RepoState",
    "RepoStateCache",
    "compute_state",
    "read_file",
    "relative_path",
]
