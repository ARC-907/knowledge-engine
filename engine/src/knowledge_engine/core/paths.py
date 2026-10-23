"""Single resolver for the engine's on-disk layout.

Every default filesystem location the engine uses is derived here, so the core
config, the pipeline database, the agent board's scope databases and its key
file can never disagree about where state lives.

Resolution (each ``KE_*`` variable wins when set to a non-empty value):

* ``home``          -- ``KE_HOME``; otherwise the first directory at or above
  the current working directory that contains ``corpus/registry.json``;
  otherwise the current working directory itself.
* ``corpus_root``   -- ``KE_CORPUS_ROOT``, else ``<home>/corpus``.
* ``registry_path`` -- ``KE_REGISTRY_PATH``, else ``<corpus_root>/registry.json``.
* ``data_dir``      -- ``KE_DATA_DIR``, else ``<home>/.knowledge-engine/data``
  (the same ``.knowledge-engine/`` convention project-context uses).

Resolution is read-only: nothing here creates, writes or deletes anything.
Entry points that write state call :func:`ensure_data_dir` (or
``Config.ensure_dirs``) explicitly.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

ENV_HOME = "KE_HOME"
ENV_CORPUS_ROOT = "KE_CORPUS_ROOT"
ENV_REGISTRY_PATH = "KE_REGISTRY_PATH"
ENV_DATA_DIR = "KE_DATA_DIR"

CORPUS_DIRNAME = "corpus"
REGISTRY_FILENAME = "registry.json"
STATE_DIRNAME = ".knowledge-engine"
DATA_DIRNAME = "data"

# Corpus roots already reported missing, so the warning is logged once per path
# rather than on every resolution.
_warned_missing_corpus: set[Path] = set()
_warn_lock = threading.Lock()


@dataclass(frozen=True)
class EnginePaths:
    """The resolved on-disk layout. All paths are absolute."""

    home: Path
    corpus_root: Path
    registry_path: Path
    data_dir: Path


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    """Return ``env[name]`` as a path, treating unset/blank values as absent."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def discover_home(start: Path | None = None) -> Path:
    """Walk upward from ``start`` (default: cwd) to the nearest engine home.

    The home is the first directory that contains ``corpus/registry.json``.
    When no ancestor qualifies, ``start`` itself is returned.
    """
    base = (start if start is not None else Path.cwd()).resolve()
    for candidate in (base, *base.parents):
        if (candidate / CORPUS_DIRNAME / REGISTRY_FILENAME).is_file():
            return candidate
    return base


def resolve_home(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path:
    """``KE_HOME`` when set, else :func:`discover_home` from ``cwd``."""
    env = os.environ if env is None else env
    explicit = _env_path(env, ENV_HOME)
    if explicit is not None:
        return explicit.resolve()
    return discover_home(cwd)


def resolve_data_dir(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path:
    """``KE_DATA_DIR`` when set, else ``<home>/.knowledge-engine/data``.

    Only walks the filesystem for the home when ``KE_DATA_DIR`` is unset.
    """
    env = os.environ if env is None else env
    explicit = _env_path(env, ENV_DATA_DIR)
    if explicit is not None:
        return explicit.resolve()
    return (resolve_home(env, cwd) / STATE_DIRNAME / DATA_DIRNAME).resolve()


def resolve(
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    *,
    warn_missing_corpus: bool = True,
) -> EnginePaths:
    """Resolve the full layout.

    Logs a WARNING (once per path) when the corpus root does not exist, unless
    ``warn_missing_corpus`` is False (for import-time callers).
    """
    env = os.environ if env is None else env
    home = resolve_home(env, cwd)
    corpus_root = (_env_path(env, ENV_CORPUS_ROOT) or home / CORPUS_DIRNAME).resolve()
    registry_path = (_env_path(env, ENV_REGISTRY_PATH) or corpus_root / REGISTRY_FILENAME).resolve()
    data_dir = (_env_path(env, ENV_DATA_DIR) or home / STATE_DIRNAME / DATA_DIRNAME).resolve()
    if warn_missing_corpus:
        _warn_if_corpus_missing(corpus_root)
    return EnginePaths(
        home=home, corpus_root=corpus_root, registry_path=registry_path, data_dir=data_dir
    )


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """Create ``data_dir`` (default: the resolved one) if needed and return it."""
    target = data_dir if data_dir is not None else resolve_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def ensure_within(root: str | Path, path: str | Path, *, label: str = "the root") -> Path:
    """Resolve ``path`` against ``root`` and require the result to stay under it.

    Relative paths are joined to ``root``; absolute paths are taken as given.
    Symlinks are followed, so a link that points outside ``root`` is rejected.
    Returns the resolved path. Raises ``ValueError`` when the path escapes
    ``root`` (``"... resolves outside <label>"``) or cannot be resolved
    (including an embedded NUL byte).
    """
    if "\x00" in str(path):
        # Windows' realpath swallows the embedded-null ValueError and returns
        # the path unchanged, so check explicitly on every platform.
        raise ValueError(f"cannot resolve {str(path)!r}: embedded null byte")
    try:
        root_resolved = Path(root).resolve()
        resolved = (root_resolved / path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"cannot resolve {str(path)!r}: {exc}") from exc
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"{str(path)!r} resolves outside {label}")
    return resolved


def _warn_if_corpus_missing(corpus_root: Path) -> None:
    if corpus_root.exists():
        return
    with _warn_lock:
        if corpus_root in _warned_missing_corpus:
            return
        _warned_missing_corpus.add(corpus_root)
    _logger.warning(
        "corpus root %s does not exist; set %s or %s, or run from a directory containing corpus/%s",
        corpus_root,
        ENV_CORPUS_ROOT,
        ENV_HOME,
        REGISTRY_FILENAME,
    )


__all__ = [
    "CORPUS_DIRNAME",
    "DATA_DIRNAME",
    "ENV_CORPUS_ROOT",
    "ENV_DATA_DIR",
    "ENV_HOME",
    "ENV_REGISTRY_PATH",
    "REGISTRY_FILENAME",
    "STATE_DIRNAME",
    "EnginePaths",
    "discover_home",
    "ensure_data_dir",
    "ensure_within",
    "resolve",
    "resolve_data_dir",
    "resolve_home",
]
