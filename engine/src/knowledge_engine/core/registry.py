"""Registry: ``corpus/registry.json`` is the single source of truth.

The document lists every library, skill, kit and tool (one bucket per kind)
plus the corpus lifecycle flags. It is held in memory, mutated under a lock
and written back atomically after every change. Earlier releases also mirrored
it into ``<data_dir>/registry.db``; nothing ever read that mirror, so it is no
longer written (an existing file is left untouched and can be deleted).
"""

from __future__ import annotations

import builtins
import contextlib
import copy
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, TextIO, TypeVar, get_args

EntryKind = Literal["library", "skill", "kit", "tool"]
#: Public name for the entry-kind vocabulary (HTTP query/body validation uses it).
RegistryKind = EntryKind
REGISTRY_KINDS: tuple[str, ...] = get_args(EntryKind)

_logger = logging.getLogger(__name__)

#: Random temp-file names collide only in theory; give up after this many tries.
_REPLACEMENT_ATTEMPTS = 100

#: Entry kind → its bucket (top-level list) in ``registry.json``.
KIND_TO_BUCKET: dict[EntryKind, str] = {
    "library": "libraries",
    "skill": "skills",
    "kit": "kits",
    "tool": "tools",
}
#: Bucket in ``registry.json`` → entry kind.
BUCKET_TO_KIND: dict[str, EntryKind] = {v: k for k, v in KIND_TO_BUCKET.items()}
#: Corpus sub-folder → the kind of entry each child folder registers as
#: (tools live under ``corpus/capabilities/``).
CORPUS_FOLDER_TO_KIND: dict[str, EntryKind] = {
    "libraries": "library",
    "skills": "skill",
    "kits": "kit",
    "capabilities": "tool",
}


_T = TypeVar("_T")


class RegistryClosedError(sqlite3.ProgrammingError):
    """A mutation was attempted on a closed :class:`Registry`.

    Subclasses :class:`sqlite3.ProgrammingError` (what a closed registry
    raised while it still held a SQLite mirror) so existing handlers keep working.
    """


@dataclass
class RegistryEntry:
    id: str
    kind: EntryKind
    name: str
    path: str  # path relative to corpus_root
    enabled: bool = True
    auto_registered: bool = False
    description: str = ""
    tags: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


def _default_document() -> dict[str, Any]:
    return {
        "version": 1,
        "libraries": [],
        "skills": [],
        "kits": [],
        "tools": [],
        "lifecycle": {"watch_enabled": True, "auto_register": True},
    }


class Registry:
    """JSON-backed registry of corpus entries.

    Thread safety: one re-entrant lock serialises every read and mutation of
    the in-memory document and the file write, so a single instance can be
    shared by FastAPI's worker threadpool. Each mutation is applied to a copy
    of the document, written to disk atomically (temp file, ``fsync``,
    ``os.replace``, keeping the file's permission bits) and only then swapped in, so a failed write leaves both
    the file and the in-memory state unchanged and a crash mid-write never
    truncates ``registry.json``.

    :meth:`close` marks the registry closed; later mutations raise
    :class:`RegistryClosedError`.

    ``db_path`` is accepted for backwards compatibility and ignored (the
    SQLite mirror is gone).
    """

    def __init__(self, registry_path: Path, db_path: Path | None = None) -> None:
        self.registry_path = Path(registry_path)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._closed = False
        self._data: dict[str, Any] = {}
        self._load()

    # ---------- Lifecycle ----------
    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Mark the registry closed. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        _logger.debug("registry %s closed", self.registry_path)

    def _check_open(self) -> None:
        if self._closed:
            raise RegistryClosedError("Cannot operate on a closed registry.")

    # ---------- JSON I/O ----------
    def _load(self) -> None:
        with self._lock:
            if not self.registry_path.exists():
                self.registry_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_json(_default_document())
            with self.registry_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(
                    f"{self.registry_path}: expected a JSON object at the top level, "
                    f"got {type(data).__name__}"
                )
            self._data = data

    def reload(self) -> None:
        """Re-read ``registry.json`` from disk (after an out-of-band edit)."""
        self._load()

    def _write_json(self, data: dict[str, Any]) -> None:
        """Write ``data`` to the registry file atomically (temp file, fsync, replace).

        The temp file is created the way a plain ``open()`` creates a new file
        (``0o666`` masked by the process umask, no explicit mode), then chmod'ed
        to the existing file's permission bits when there is one, so an
        operator-edited or bind-mounted ``registry.json`` keeps its mode across
        persists instead of inheriting the private ``0600`` a ``mkstemp`` temp
        file would have. ``newline="\n"`` keeps the file LF on every platform
        (matching ``.gitattributes``); fsync makes the replace durable.
        """
        text = json.dumps(data, indent=2)
        try:
            mode: int | None = stat.S_IMODE(self.registry_path.stat().st_mode)
        except FileNotFoundError:
            mode = None
        handle, tmp = self._open_replacement()
        try:
            with handle:
                if mode is not None:
                    tmp.chmod(mode)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.registry_path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def _open_replacement(self) -> tuple[TextIO, Path]:
        """Create a fresh ``.<name>.<random>.tmp`` next to the registry file.

        Like ``tempfile.mkstemp`` (exclusive creation on a random name) but
        through ``open(..., "x")`` so the file gets the umask default instead
        of mkstemp's fixed ``0600``; the caller copies an existing registry
        file's mode over afterwards. The text wrapper's ``newline="\n"`` keeps
        line endings LF on Windows too.
        """
        directory = self.registry_path.parent
        for _attempt in range(_REPLACEMENT_ATTEMPTS):
            tmp = directory / f".{self.registry_path.name}.{secrets.token_hex(8)}.tmp"
            try:
                handle = open(tmp, "x", encoding="utf-8", newline="\n")  # noqa: SIM115, PTH123
            except FileExistsError:
                continue
            return handle, tmp
        raise FileExistsError(
            f"could not create a temp file for {self.registry_path} in {directory}"
        )

    def _mutate(self, change: Callable[[dict[str, Any]], _T]) -> _T:
        """Apply ``change`` to a copy of the document, persist it, then swap it in.

        ``change`` returns the call's result; it may raise to abort without
        writing. The caller holds no lock; this method takes it.
        """
        with self._lock:
            self._check_open()
            draft = copy.deepcopy(self._data)
            result = change(draft)
            draft["last_updated"] = time.time()
            self._write_json(draft)
            self._data = draft
            return result

    # ---------- Public API ----------
    def list(
        self, kind: EntryKind | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        with self._lock:
            results: list[dict[str, Any]] = []
            kinds: Iterable[EntryKind] = [kind] if kind else KIND_TO_BUCKET.keys()
            for k in kinds:
                for row in self._data.get(KIND_TO_BUCKET[k], []):
                    if enabled_only and not row.get("enabled", True):
                        continue
                    results.append({**row, "kind": k})
            return results

    def counts(self) -> dict[str, int]:
        """Entries per bucket: ``{"libraries": n, "skills": n, "kits": n, "tools": n}``."""
        with self._lock:
            return {bucket: len(self._data.get(bucket, [])) for bucket in BUCKET_TO_KIND}

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            for bucket, kind in BUCKET_TO_KIND.items():
                for row in self._data.get(bucket, []):
                    if row["id"] == entry_id:
                        return {**row, "kind": kind}
            return None

    @staticmethod
    def _upsert_into(data: dict[str, Any], entry: RegistryEntry) -> dict[str, Any]:
        rows: list[dict[str, Any]] = data.setdefault(KIND_TO_BUCKET[entry.kind], [])
        payload = asdict(entry)
        payload["last_updated"] = time.time()
        for i, row in enumerate(rows):
            if row["id"] == entry.id:
                rows[i] = {**row, **payload}
                return dict(rows[i])
        rows.append(payload)
        return dict(payload)

    def upsert(self, entry: RegistryEntry) -> dict[str, Any]:
        """Insert ``entry`` or update the row with the same id (one atomic write)."""
        return self._mutate(lambda data: self._upsert_into(data, entry))

    def upsert_many(self, entries: Iterable[RegistryEntry]) -> builtins.list[dict[str, Any]]:
        """Upsert several entries with a single atomic write (none on an empty input)."""
        batch = list(entries)
        if not batch:
            return []
        return self._mutate(lambda data: [self._upsert_into(data, entry) for entry in batch])

    def set_enabled(self, entry_id: str, enabled: bool) -> dict[str, Any] | None:
        def change(data: dict[str, Any]) -> dict[str, Any] | None:
            for bucket, kind in BUCKET_TO_KIND.items():
                for row in data.get(bucket, []):
                    if row["id"] == entry_id:
                        row["enabled"] = bool(enabled)
                        row["last_updated"] = time.time()
                        return {**row, "kind": kind}
            return None

        with self._lock:
            self._check_open()
            if self.get(entry_id) is None:
                return None
            return self._mutate(change)

    def remove(self, entry_id: str) -> bool:
        def change(data: dict[str, Any]) -> bool:
            for bucket in BUCKET_TO_KIND:
                rows = data.get(bucket, [])
                for i, row in enumerate(rows):
                    if row["id"] == entry_id:
                        rows.pop(i)
                        return True
            return False

        with self._lock:
            self._check_open()
            if self.get(entry_id) is None:
                return False
            return self._mutate(change)

    def lifecycle(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("lifecycle", {}))

    def set_lifecycle(self, **kwargs: Any) -> dict[str, Any]:
        def change(data: dict[str, Any]) -> dict[str, Any]:
            lc = data.setdefault("lifecycle", {})
            lc.update(kwargs)
            return dict(lc)

        return self._mutate(change)


__all__ = [
    "BUCKET_TO_KIND",
    "CORPUS_FOLDER_TO_KIND",
    "KIND_TO_BUCKET",
    "REGISTRY_KINDS",
    "EntryKind",
    "Registry",
    "RegistryClosedError",
    "RegistryEntry",
    "RegistryKind",
]
