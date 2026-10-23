"""Registry: ``corpus/registry.json`` is the single source of truth.

The document lists every library, skill, kit and tool (one bucket per kind)
plus the corpus lifecycle flags. It is held in memory, mutated under a lock
and written back atomically after every change. Earlier releases also mirrored
it into ``<data_dir>/registry.db``; nothing ever read that mirror, so it is no
longer written (an existing file is left untouched and can be deleted).

The document is validated against the JSON Schema shipped with the package
(``knowledge_engine/core/registry.schema.json``, Draft 2020-12; the repo keeps
an identical copy at ``corpus/registry.schema.json``) every time it is loaded
and before every write. An invalid document raises
:class:`RegistryValidationError`, which lists every problem with its JSON
pointer; nothing is written and the in-memory state is left as it was.
"""

from __future__ import annotations

import builtins
import contextlib
import copy
import functools
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO, TypeVar, get_args

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

EntryKind = Literal["library", "skill", "kit", "tool"]
#: Public name for the entry-kind vocabulary (HTTP query/body validation uses it).
RegistryKind = EntryKind
REGISTRY_KINDS: tuple[str, ...] = get_args(EntryKind)

_logger = logging.getLogger(__name__)

#: Random temp-file names collide only in theory; give up after this many tries.
_REPLACEMENT_ATTEMPTS = 100

#: File name of the registry JSON Schema, shipped in this package.
SCHEMA_FILENAME = "registry.schema.json"

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


# ---------- Schema validation ----------


@dataclass(frozen=True)
class RegistryValidationIssue:
    """One problem found in a registry document.

    ``pointer`` is the RFC 6901 JSON pointer of the offending value (``""``
    for the document itself), ``message`` the validator's explanation and
    ``keyword`` the schema keyword that failed (``"json"`` for a parse error).
    """

    pointer: str
    message: str
    keyword: str = ""

    def __str__(self) -> str:
        return f"{self.pointer or '/'}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"pointer": self.pointer, "message": self.message, "keyword": self.keyword}


class RegistryValidationError(ValueError):
    """``registry.json`` (or a document about to be written to it) is invalid.

    ``path`` is the registry file, ``message`` a one-line summary and
    ``errors`` every :class:`RegistryValidationIssue` found, ordered by JSON
    pointer. ``str()`` renders the summary followed by one line per issue.
    Subclasses :class:`ValueError`, which an unreadable registry raised before.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        message: str,
        errors: Sequence[RegistryValidationIssue] = (),
    ) -> None:
        self.path = Path(path)
        self.message = message
        self.errors: tuple[RegistryValidationIssue, ...] = tuple(errors)
        super().__init__(self.render())

    def render(self) -> str:
        lines = [f"{self.path}: {self.message}"]
        lines.extend(f"  {issue}" for issue in self.errors)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "message": self.message,
            "errors": [issue.to_dict() for issue in self.errors],
        }


def _json_pointer(parts: Iterable[str | int]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def registry_schema() -> dict[str, Any]:
    """The registry JSON Schema (a fresh copy of the package's ``registry.schema.json``)."""
    return copy.deepcopy(_schema())


@functools.cache
def _schema() -> dict[str, Any]:
    package = __name__.rpartition(".")[0]
    text = resources.files(package).joinpath(SCHEMA_FILENAME).read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


@functools.cache
def _validator() -> Validator:
    # jsonschema is imported on first use so importing the registry stays cheap.
    from jsonschema import Draft202012Validator  # noqa: PLC0415

    schema = _schema()
    Draft202012Validator.check_schema(schema)
    validator: Validator = Draft202012Validator(schema)
    return validator


def validation_issues(document: Any) -> list[RegistryValidationIssue]:
    """Every schema violation in ``document``, ordered by JSON pointer (empty if valid)."""
    issues = [
        RegistryValidationIssue(
            pointer=_json_pointer(error.absolute_path),
            message=error.message,
            keyword=str(error.validator),
        )
        for error in _validator().iter_errors(document)
    ]
    issues.sort(key=lambda issue: (issue.pointer, issue.keyword, issue.message))
    return issues


def validate_registry_document(
    document: Any, path: str | os.PathLike[str] = "registry.json"
) -> None:
    """Raise :class:`RegistryValidationError` unless ``document`` matches the schema."""
    issues = validation_issues(document)
    if issues:
        noun = "error" if len(issues) == 1 else "errors"
        raise RegistryValidationError(
            path, f"does not match the registry schema ({len(issues)} {noun})", issues
        )


def load_registry_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and validate a registry file without creating or modifying anything.

    Raises :class:`FileNotFoundError` (or another :class:`OSError`) when the file
    cannot be read and :class:`RegistryValidationError` when it is not valid
    JSON or does not match the schema.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        issue = RegistryValidationIssue(
            pointer="", message=f"{exc.msg} (line {exc.lineno}, column {exc.colno})", keyword="json"
        )
        raise RegistryValidationError(path, "is not valid JSON", [issue]) from exc
    if not isinstance(data, dict):
        raise RegistryValidationError(
            path,
            f"expected a JSON object at the top level, got {type(data).__name__}",
            [RegistryValidationIssue("", f"{type(data).__name__} is not an object", "type")],
        )
    validate_registry_document(data, path)
    return data


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

    The document is validated against the registry schema on every load
    (construction and :meth:`reload`) and before every write; an invalid one
    raises :class:`RegistryValidationError` (logged at ERROR) and changes
    nothing.

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
            try:
                data = load_registry_document(self.registry_path)
            except RegistryValidationError as exc:
                _log_invalid(exc)
                raise
            self._data = data

    def reload(self) -> None:
        """Re-read ``registry.json`` from disk (after an out-of-band edit).

        If the file is now invalid, :class:`RegistryValidationError` is raised
        and the previously loaded document stays in effect.
        """
        self._load()

    def _validate(self, data: dict[str, Any]) -> None:
        """Refuse to write a document that the next load would reject."""
        try:
            validate_registry_document(data, self.registry_path)
        except RegistryValidationError as exc:
            _log_invalid(exc)
            raise

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
        writing, and so does a draft that fails schema validation. The caller
        holds no lock; this method takes it.
        """
        with self._lock:
            self._check_open()
            draft = copy.deepcopy(self._data)
            result = change(draft)
            draft["last_updated"] = time.time()
            self._validate(draft)
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


def _log_invalid(exc: RegistryValidationError) -> None:
    _logger.error(
        "registry %s %s: %s",
        exc.path,
        exc.message,
        "; ".join(str(issue) for issue in exc.errors),
    )


__all__ = [
    "BUCKET_TO_KIND",
    "CORPUS_FOLDER_TO_KIND",
    "KIND_TO_BUCKET",
    "REGISTRY_KINDS",
    "SCHEMA_FILENAME",
    "EntryKind",
    "Registry",
    "RegistryClosedError",
    "RegistryEntry",
    "RegistryKind",
    "RegistryValidationError",
    "RegistryValidationIssue",
    "load_registry_document",
    "registry_schema",
    "validate_registry_document",
    "validation_issues",
]
