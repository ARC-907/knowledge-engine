"""The scoped-knowledge metadata store (``knowledge.db``) and its repository API.

``knowledge.db`` (``KE_KNOWLEDGE_DB``, default ``<KE_DATA_DIR>/knowledge.db``)
owns workspaces, projects, sources, collections, collection membership,
per-record classification (research type, normalized tags, curation status),
scope profiles and backfill checkpoints. Its schema is the numbered SQL in
``knowledge/migrations/`` applied by the shared runner
(:func:`knowledge_engine.core.util.sqlite.apply_migrations`). Opening a
database whose schema is newer than this binary knows raises
:class:`~knowledge_engine.knowledge.errors.IncompatibleError` instead of
touching it.

Nothing is created implicitly by a query: the default workspace and the
legacy corpus source are created explicitly by the backfill
(:mod:`knowledge_engine.knowledge.backfill`) or by an admin call.

Like the FTS ``Indexer``, a store holds one connection guarded by a lock, so
a single instance can be shared by FastAPI's worker threads. Every method
validates its input and raises typed
:class:`~knowledge_engine.knowledge.errors.KnowledgeError` subclasses.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from ..core.util.sqlite import (
    apply_migrations,
    connect,
    discover_migrations,
    schema_version as read_schema_version,
)
from .contracts import (
    MAX_SELECTION,
    Bibliographic,
    Collection,
    CollectionId,
    CurationStatus,
    Description,
    DisplayName,
    EffectiveScope,
    Project,
    ProjectId,
    QueryContext,
    RecordId,
    RecordScope,
    ResearchType,
    RetentionMode,
    RightsInfo,
    ScopeProfile,
    Source,
    SourceId,
    SourceRecord,
    Workspace,
    WorkspaceId,
    normalize_tags,
    validation_messages,
)
from .errors import (
    IncompatibleError,
    InvalidInputError,
    InvalidScopeError,
    KnowledgeError,
    UnavailableError,
    UnknownScopeError,
)
from .scope import DEFAULT_PROFILE_ID, RecordView, ScopeCatalog, resolve_scope

_logger = logging.getLogger(__name__)

KNOWLEDGE_DB_FILENAME = "knowledge.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: Source kinds the store accepts (``other`` for anything not yet modelled).
SOURCE_KINDS = ("corpus", "project_context", "board", "serena", "module", "other")
#: Largest page :meth:`KnowledgeStore.list_records` returns.
MAX_PAGE = 500
#: Most in-scope records one search snapshot loads (bounded memory per request).
MAX_SNAPSHOT_RECORDS = 1_000_000
#: Largest JSON configuration a source may store.
MAX_SOURCE_CONFIG_BYTES = 262_144

_T = TypeVar("_T")


def latest_schema_version() -> int:
    """The newest ``knowledge.db`` schema version this binary ships."""
    return max((m.version for m in discover_migrations(MIGRATIONS_DIR)), default=0)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _check[V](adapter: TypeAdapter[V], value: Any, field: str) -> V:
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise InvalidInputError(
            f"invalid {field}: {value!r}",
            details={"field": field, "errors": validation_messages(exc)},
        ) from exc


_WORKSPACE_ID = TypeAdapter(WorkspaceId)
_PROJECT_ID = TypeAdapter(ProjectId)
_COLLECTION_ID = TypeAdapter(CollectionId)
_SOURCE_ID = TypeAdapter(SourceId)
_RECORD_ID = TypeAdapter(RecordId)
_DISPLAY_NAME = TypeAdapter(DisplayName)
_DESCRIPTION = TypeAdapter(Description)
_RESEARCH_TYPE = TypeAdapter(ResearchType)
_CURATION = TypeAdapter(CurationStatus)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


class _Unset:
    """Sentinel: "leave unchanged" (``None`` means "clear")."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


@dataclass(frozen=True)
class RecordRef:
    """``(source_id, record_id)``: the identity of a record."""

    source_id: str
    record_id: str

    @classmethod
    def parse(cls, value: Any) -> RecordRef:
        if isinstance(value, RecordRef):
            return value
        if isinstance(value, dict):
            source, record = value.get("source_id"), value.get("record_id")
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            source, record = value
        else:
            raise InvalidInputError(
                "a record reference is {source_id, record_id}", details={"value": repr(value)}
            )
        return cls(_check(_SOURCE_ID, source, "source_id"), _check(_RECORD_ID, record, "record_id"))


@dataclass(frozen=True)
class NewRecord:
    """A record to register (used by the backfill)."""

    record_id: str
    logical_path: str | None
    title: str = ""
    canonical_uri: str | None = None


@dataclass(frozen=True)
class SnapshotRecord:
    """One in-scope record of a :class:`ScopeSnapshot`: its scope view, path and title."""

    view: RecordView
    logical_path: str | None
    title: str


@dataclass(frozen=True)
class SnapshotSource:
    """A source of the snapshot's workspace: its kind and stored configuration."""

    source_id: str
    kind: str
    display_name: str
    config: dict[str, Any]


@dataclass(frozen=True)
class ScopeSnapshot:
    """A consistent view of one request's scope, read in one transaction.

    ``records`` holds only the records inside ``scope`` (by source, then
    record id); records of sources that keep no rows in ``knowledge.db``
    (project context) are absent and their adapters apply
    :func:`~knowledge_engine.knowledge.scope.record_in_scope` themselves;
    ``record_sources`` names the sources that do keep rows, so an empty
    in-scope set of such a source still means "nothing visible".
    """

    scope: EffectiveScope
    sources: dict[str, SnapshotSource]
    records: dict[str, dict[str, SnapshotRecord]]
    #: Sources of the workspace that keep record rows in ``knowledge.db``.
    record_sources: frozenset[str] = frozenset()

    def by_path(self, source_id: str) -> dict[str, SnapshotRecord]:
        """``{logical_path: record}`` of one source's in-scope records."""
        return {
            rec.logical_path: rec
            for rec in self.records.get(source_id, {}).values()
            if rec.logical_path is not None
        }


class KnowledgeStore:
    """Repository over one ``knowledge.db``. Use :meth:`open` (migrates) or :meth:`open_readonly`."""

    def __init__(self, conn: sqlite3.Connection, path: Path, *, readonly: bool) -> None:
        self._conn = conn
        self.path = path
        self.readonly = readonly
        self._lock = threading.RLock()
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────

    @classmethod
    def open(cls, path: Path | str) -> KnowledgeStore:
        """Open (creating if needed) and migrate ``path`` to the latest schema."""
        target = Path(path)
        conn = connect(target)
        try:
            _refuse_newer(conn, target)
            applied = apply_migrations(conn, MIGRATIONS_DIR)
        except BaseException:
            conn.close()
            raise
        if applied:
            _logger.info("knowledge store %s migrated (%d migration(s))", target, applied)
        return cls(conn, target, readonly=False)

    @classmethod
    def open_readonly(cls, path: Path | str) -> KnowledgeStore:
        """Open an existing database without writing anything (no migrations)."""
        target = Path(path)
        if not target.is_file():
            raise UnavailableError(
                "the knowledge store does not exist yet",
                details={"hint": "run `knowledge-engine knowledge migrate --apply`"},
            )
        uri = target.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            _refuse_newer(conn, target)
        except BaseException:
            conn.close()
            raise
        return cls(conn, target, readonly=True)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the connection. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
        _logger.debug("knowledge store %s closed", self.path)

    def __enter__(self) -> KnowledgeStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def schema_version(self) -> int:
        with self._reading() as conn:
            return read_schema_version(conn)

    # ── transactions ─────────────────────────────────────────────

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """A consistent read snapshot (one deferred transaction)."""
        with self._lock:
            self._check_open()
            began = not self._conn.in_transaction
            if began:
                self._conn.execute("BEGIN")
            try:
                yield self._conn
            finally:
                if began and self._conn.in_transaction:
                    self._conn.rollback()

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; rolled back on any error."""
        with self._lock:
            self._check_open()
            if self.readonly:
                raise InvalidInputError("the knowledge store is open read-only")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """A consistent read snapshot for sibling repositories (the evidence store)."""
        with self._reading() as conn:
            yield conn

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction for sibling repositories; rolled back on any error."""
        with self._writing() as conn:
            yield conn

    def guarded(self, fn: Callable[[], _T]) -> _T:
        """Run ``fn`` translating SQLite failures into typed knowledge errors."""
        return self._guard(fn)

    def _check_open(self) -> None:
        if self._closed:
            raise UnavailableError("the knowledge store is closed")

    def _guard(self, fn: Callable[[], _T]) -> _T:
        try:
            return fn()
        except KnowledgeError:
            raise
        except sqlite3.IntegrityError as exc:
            raise InvalidInputError(f"constraint violated: {exc}") from exc
        except sqlite3.OperationalError as exc:
            _logger.error("knowledge store %s: %s", self.path, exc)
            raise UnavailableError("the knowledge store is unavailable; retry later") from exc

    # ── workspaces ───────────────────────────────────────────────

    def create_workspace(
        self, display_name: str, *, workspace_id: str | None = None, description: str = ""
    ) -> Workspace:
        """Create a workspace (``workspace_id`` generated when omitted); an existing id fails."""
        wid = _check(_WORKSPACE_ID, workspace_id or _new_id("ws"), "workspace_id")
        name = _check(_DISPLAY_NAME, display_name, "display_name")
        desc = _check(_DESCRIPTION, description, "description")

        def run() -> Workspace:
            with self._writing() as conn:
                if _exists(conn, "workspaces", "workspace_id", wid):
                    raise InvalidInputError(
                        f"workspace {wid!r} already exists",
                        details={"workspace_id": wid, "reason": "exists"},
                    )
                now = _now()
                conn.execute(
                    "INSERT INTO workspaces (workspace_id, display_name, description, created_at,"
                    " updated_at) VALUES (?, ?, ?, ?, ?)",
                    (wid, name, desc, now, now),
                )
                return self._workspace(conn, wid)

        workspace = self._guard(run)
        _logger.info("knowledge: created workspace %s", wid)
        return workspace

    def ensure_workspace(self, workspace_id: str, display_name: str) -> tuple[Workspace, bool]:
        """The workspace ``workspace_id``, created with ``display_name`` if missing."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        with self._reading() as conn:
            if _exists(conn, "workspaces", "workspace_id", wid):
                return self._workspace(conn, wid), False
        try:
            return self.create_workspace(display_name, workspace_id=wid), True
        except InvalidInputError as exc:  # created concurrently
            if exc.details.get("reason") != "exists":
                raise
            return self.get_workspace(wid), False

    def get_workspace(self, workspace_id: str) -> Workspace:
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        with self._reading() as conn:
            return self._workspace(conn, wid)

    def list_workspaces(self) -> list[Workspace]:
        with self._reading() as conn:
            rows = conn.execute("SELECT * FROM workspaces ORDER BY workspace_id").fetchall()
        return [_workspace_model(row) for row in rows]

    def rename_workspace(self, workspace_id: str, display_name: str) -> Workspace:
        """Change the display name; the id (the identity) never changes."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        name = _check(_DISPLAY_NAME, display_name, "display_name")

        def run() -> Workspace:
            with self._writing() as conn:
                self._workspace(conn, wid)
                conn.execute(
                    "UPDATE workspaces SET display_name = ?, updated_at = ? WHERE workspace_id = ?",
                    (name, _now(), wid),
                )
                return self._workspace(conn, wid)

        return self._guard(run)

    def _workspace(self, conn: sqlite3.Connection, wid: str) -> Workspace:
        row = conn.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (wid,)).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown workspace {wid!r}", details={"kind": "workspace", "id": wid}
            )
        return _workspace_model(row)

    # ── projects ─────────────────────────────────────────────────

    def create_project(
        self,
        workspace_id: str,
        display_name: str,
        *,
        project_id: str | None = None,
        description: str = "",
    ) -> Project:
        """Create a project. Display names may repeat; the opaque id is the identity."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        pid = _check(_PROJECT_ID, project_id or _new_id("prj"), "project_id")
        name = _check(_DISPLAY_NAME, display_name, "display_name")
        desc = _check(_DESCRIPTION, description, "description")

        def run() -> Project:
            with self._writing() as conn:
                self._workspace(conn, wid)
                if _exists(conn, "projects", "project_id", pid):
                    raise InvalidInputError(
                        f"project {pid!r} already exists",
                        details={"project_id": pid, "reason": "exists"},
                    )
                now = _now()
                conn.execute(
                    "INSERT INTO projects (project_id, workspace_id, display_name, description,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, wid, name, desc, now, now),
                )
                return self._project(conn, pid)

        project = self._guard(run)
        _logger.info("knowledge: created project %s in %s", pid, wid)
        return project

    def get_project(self, project_id: str) -> Project:
        pid = _check(_PROJECT_ID, project_id, "project_id")
        with self._reading() as conn:
            return self._project(conn, pid)

    def list_projects(self, workspace_id: str) -> list[Project]:
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        with self._reading() as conn:
            self._workspace(conn, wid)
            rows = conn.execute(
                "SELECT * FROM projects WHERE workspace_id = ? ORDER BY display_name, project_id",
                (wid,),
            ).fetchall()
        return [_project_model(row) for row in rows]

    def _project(self, conn: sqlite3.Connection, pid: str) -> Project:
        row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (pid,)).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown project {pid!r}", details={"kind": "project", "id": pid}
            )
        return _project_model(row)

    # ── sources ──────────────────────────────────────────────────

    def register_source(
        self,
        workspace_id: str,
        source_id: str,
        *,
        kind: str,
        display_name: str,
        config: dict[str, Any] | None = None,
    ) -> tuple[Source, bool]:
        """Register a source in a workspace; idempotent for the same workspace and kind."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        sid = _check(_SOURCE_ID, source_id, "source_id")
        name = _check(_DISPLAY_NAME, display_name, "display_name")
        if kind not in SOURCE_KINDS:
            raise InvalidInputError(
                f"unknown source kind {kind!r}", details={"allowed": list(SOURCE_KINDS)}
            )

        def run() -> tuple[Source, bool]:
            with self._writing() as conn:
                self._workspace(conn, wid)
                row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (sid,)).fetchone()
                if row is not None:
                    if row["workspace_id"] != wid or row["kind"] != kind:
                        raise InvalidInputError(
                            f"source {sid!r} is already registered in workspace "
                            f"{row['workspace_id']!r} as kind {row['kind']!r}",
                            details={"source_id": sid, "reason": "exists"},
                        )
                    return _source_model(row), False
                now = _now()
                conn.execute(
                    "INSERT INTO sources (source_id, workspace_id, kind, display_name, config_json,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, wid, kind, name, json.dumps(config or {}, sort_keys=True), now, now),
                )
                return self._source(conn, sid), True

        return self._guard(run)

    def get_source(self, source_id: str) -> Source:
        sid = _check(_SOURCE_ID, source_id, "source_id")
        with self._reading() as conn:
            return self._source(conn, sid)

    def list_sources(self, workspace_id: str) -> list[Source]:
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        with self._reading() as conn:
            self._workspace(conn, wid)
            rows = conn.execute(
                "SELECT * FROM sources WHERE workspace_id = ? ORDER BY source_id", (wid,)
            ).fetchall()
        return [_source_model(row) for row in rows]

    def _source(self, conn: sqlite3.Connection, sid: str) -> Source:
        row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (sid,)).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown source {sid!r}", details={"kind": "source", "id": sid}
            )
        return _source_model(row)

    # ── collections ──────────────────────────────────────────────

    def create_collection(
        self,
        workspace_id: str,
        display_name: str,
        *,
        collection_id: str | None = None,
        project_id: str | None = None,
        description: str = "",
    ) -> Collection:
        """Create a collection (workspace-wide, or belonging to one project)."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        cid = _check(_COLLECTION_ID, collection_id or _new_id("col"), "collection_id")
        pid = _check(_PROJECT_ID, project_id, "project_id") if project_id is not None else None
        name = _check(_DISPLAY_NAME, display_name, "display_name")
        desc = _check(_DESCRIPTION, description, "description")

        def run() -> Collection:
            with self._writing() as conn:
                self._workspace(conn, wid)
                if pid is not None and self._project(conn, pid).workspace_id != wid:
                    raise InvalidScopeError(
                        f"project {pid!r} belongs to another workspace",
                        details={"project_id": pid, "workspace_id": wid},
                    )
                if _exists(conn, "collections", "collection_id", cid):
                    raise InvalidInputError(
                        f"collection {cid!r} already exists",
                        details={"collection_id": cid, "reason": "exists"},
                    )
                now = _now()
                conn.execute(
                    "INSERT INTO collections (collection_id, workspace_id, project_id, display_name,"
                    " description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cid, wid, pid, name, desc, now, now),
                )
                return self._collection(conn, cid)

        collection = self._guard(run)
        _logger.info("knowledge: created collection %s in %s", cid, wid)
        return collection

    def get_collection(self, collection_id: str) -> Collection:
        cid = _check(_COLLECTION_ID, collection_id, "collection_id")
        with self._reading() as conn:
            return self._collection(conn, cid)

    def list_collections(
        self, workspace_id: str, *, project_id: str | None = None
    ) -> list[Collection]:
        """Collections of a workspace (only one project's when ``project_id`` is given)."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        pid = _check(_PROJECT_ID, project_id, "project_id") if project_id is not None else None
        sql = (
            "SELECT c.*, (SELECT COUNT(*) FROM collection_members m"
            " WHERE m.collection_id = c.collection_id) AS member_count"
            " FROM collections c WHERE c.workspace_id = ?"
        )
        params: list[Any] = [wid]
        with self._reading() as conn:
            self._workspace(conn, wid)
            if pid is not None:
                self._project(conn, pid)
                sql += " AND c.project_id = ?"
                params.append(pid)
            rows = conn.execute(
                sql + " ORDER BY c.display_name, c.collection_id", params
            ).fetchall()
        return [_collection_model(row) for row in rows]

    def _collection(self, conn: sqlite3.Connection, cid: str) -> Collection:
        row = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM collection_members m"
            " WHERE m.collection_id = c.collection_id) AS member_count"
            " FROM collections c WHERE c.collection_id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown collection {cid!r}", details={"kind": "collection", "id": cid}
            )
        return _collection_model(row)

    def add_members(self, collection_id: str, refs: Iterable[Any]) -> dict[str, int]:
        """Add records to a collection; already-present members are left alone.

        A record may belong to any number of collections. Every record must
        live in the collection's workspace, and in a project collection it must
        belong to that project or to no project (workspace-wide content).
        """
        cid = _check(_COLLECTION_ID, collection_id, "collection_id")
        parsed = _parse_refs(refs)

        def run() -> dict[str, int]:
            with self._writing() as conn:
                collection = self._collection(conn, cid)
                added = 0
                now = _now()
                for ref in parsed:
                    record = self._record_row(conn, ref)
                    if record["workspace_id"] != collection.workspace_id:
                        raise InvalidScopeError(
                            f"record {ref.source_id}/{ref.record_id} is in another workspace",
                            details={"collection_id": cid},
                        )
                    if collection.project_id is not None and record["project_id"] not in (
                        None,
                        collection.project_id,
                    ):
                        raise InvalidScopeError(
                            f"record {ref.source_id}/{ref.record_id} belongs to another project",
                            details={"collection_id": cid, "project_id": record["project_id"]},
                        )
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO collection_members (collection_id, source_id,"
                        " record_id, added_at) VALUES (?, ?, ?, ?)",
                        (cid, ref.source_id, ref.record_id, now),
                    )
                    added += cur.rowcount
                conn.execute(
                    "UPDATE collections SET updated_at = ? WHERE collection_id = ?", (now, cid)
                )
                return {"added": added, "unchanged": len(parsed) - added}

        return self._guard(run)

    def remove_members(self, collection_id: str, refs: Iterable[Any]) -> dict[str, int]:
        """Remove records from a collection (records that are not members are ignored)."""
        cid = _check(_COLLECTION_ID, collection_id, "collection_id")
        parsed = _parse_refs(refs)

        def run() -> dict[str, int]:
            with self._writing() as conn:
                self._collection(conn, cid)
                removed = 0
                for ref in parsed:
                    cur = conn.execute(
                        "DELETE FROM collection_members WHERE collection_id = ? AND source_id = ?"
                        " AND record_id = ?",
                        (cid, ref.source_id, ref.record_id),
                    )
                    removed += cur.rowcount
                return {"removed": removed, "unchanged": len(parsed) - removed}

        return self._guard(run)

    # ── records ──────────────────────────────────────────────────

    def get_record(self, source_id: str, record_id: str) -> SourceRecord:
        ref = RecordRef.parse((source_id, record_id))
        with self._reading() as conn:
            row = self._record_row(conn, ref)
            return self._records_with_labels(conn, [row])[0]

    def _record_row(self, conn: sqlite3.Connection, ref: RecordRef) -> sqlite3.Row:
        row = conn.execute(
            "SELECT r.*, s.workspace_id FROM records r JOIN sources s USING (source_id)"
            " WHERE r.source_id = ? AND r.record_id = ?",
            (ref.source_id, ref.record_id),
        ).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown record {ref.source_id}/{ref.record_id}",
                details={"kind": "record", "source_id": ref.source_id, "id": ref.record_id},
            )
        found: sqlite3.Row = row
        return found

    def classify(
        self,
        source_id: str,
        record_id: str,
        *,
        research_type: str | None = None,
        curation_status: str | None = None,
        tags: Sequence[str] | None = None,
        add_tags: Sequence[str] = (),
        remove_tags: Sequence[str] = (),
        project_id: Any = UNSET,
    ) -> SourceRecord:
        """Update a record's classification; omitted fields are unchanged.

        ``tags`` replaces the tag set; ``add_tags``/``remove_tags`` edit it.
        ``project_id`` assigns the record to a project (``None`` makes it
        workspace-wide again); a record that is a member of another project's
        collection cannot move.
        """
        ref = RecordRef.parse((source_id, record_id))
        rtype = _check(_RESEARCH_TYPE, research_type, "research_type") if research_type else None
        status = _check(_CURATION, curation_status, "curation_status") if curation_status else None
        try:
            replace = normalize_tags(tags) if tags is not None else None
            plus = set(normalize_tags(add_tags))
            minus = set(normalize_tags(remove_tags))
        except ValueError as exc:
            raise InvalidInputError(str(exc), details={"field": "tags"}) from exc
        if replace is not None and (plus or minus):
            raise InvalidInputError("pass either tags or add_tags/remove_tags, not both")
        new_project: str | _Unset | None = project_id
        if project_id is not UNSET and project_id is not None:
            new_project = _check(_PROJECT_ID, project_id, "project_id")

        def run() -> SourceRecord:
            with self._writing() as conn:
                row = self._record_row(conn, ref)
                updates: dict[str, Any] = {}
                if rtype is not None:
                    updates["research_type"] = rtype.value
                if status is not None:
                    updates["curation_status"] = status.value
                if not isinstance(new_project, _Unset):
                    self._check_project_move(conn, ref, row["workspace_id"], new_project)
                    updates["project_id"] = new_project
                key = (ref.source_id, ref.record_id)
                if replace is not None:
                    conn.execute(
                        "DELETE FROM record_tags WHERE source_id = ? AND record_id = ?", key
                    )
                    plus_tags: Iterable[str] = replace
                else:
                    plus_tags = plus
                    for tag in sorted(minus):
                        conn.execute(
                            "DELETE FROM record_tags WHERE source_id = ? AND record_id = ?"
                            " AND tag = ?",
                            (*key, tag),
                        )
                conn.executemany(
                    "INSERT OR IGNORE INTO record_tags (source_id, record_id, tag) VALUES (?, ?, ?)",
                    [(*key, tag) for tag in sorted(plus_tags)],
                )
                updates["updated_at"] = _now()
                assignments = ", ".join(f"{column} = ?" for column in updates)
                conn.execute(
                    f"UPDATE records SET {assignments} WHERE source_id = ? AND record_id = ?",
                    (*updates.values(), *key),
                )
                fresh = self._record_row(conn, ref)
                return self._records_with_labels(conn, [fresh])[0]

        record = self._guard(run)
        _logger.info("knowledge: classified %s/%s", ref.source_id, ref.record_id)
        return record

    def _check_project_move(
        self, conn: sqlite3.Connection, ref: RecordRef, workspace_id: str, project_id: str | None
    ) -> None:
        if project_id is not None and self._project(conn, project_id).workspace_id != workspace_id:
            raise InvalidScopeError(
                f"project {project_id!r} belongs to another workspace",
                details={"project_id": project_id},
            )
        rows = conn.execute(
            "SELECT c.collection_id, c.project_id FROM collection_members m"
            " JOIN collections c USING (collection_id)"
            " WHERE m.source_id = ? AND m.record_id = ? AND c.project_id IS NOT NULL",
            (ref.source_id, ref.record_id),
        ).fetchall()
        conflicts = sorted(r["collection_id"] for r in rows if r["project_id"] != project_id)
        if project_id is not None and conflicts:
            raise InvalidScopeError(
                "the record is a member of another project's collection(s): "
                + ", ".join(conflicts),
                details={"collection_ids": conflicts},
            )

    def register_records(self, source_id: str, records: Sequence[NewRecord]) -> tuple[int, int]:
        """Insert records that do not exist yet; return ``(created, existing)``.

        New records are ``unclassified`` with research type ``other`` and no
        project. Existing records are never modified (their curation wins).
        """
        sid = _check(_SOURCE_ID, source_id, "source_id")

        def run() -> tuple[int, int]:
            with self._writing() as conn:
                return self._insert_records(conn, sid, records)

        return self._guard(run)

    def _insert_records(
        self, conn: sqlite3.Connection, source_id: str, records: Sequence[NewRecord]
    ) -> tuple[int, int]:
        self._source(conn, source_id)
        now = _now()
        created = 0
        for item in records:
            _check(_RECORD_ID, item.record_id, "record_id")
            cur = conn.execute(
                "INSERT INTO records (source_id, record_id, logical_path, canonical_uri, title,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (
                    source_id,
                    item.record_id,
                    item.logical_path,
                    item.canonical_uri,
                    item.title,
                    now,
                    now,
                ),
            )
            created += cur.rowcount
        return created, len(records) - created

    def record_keys(self, source_id: str) -> dict[str, str]:
        """``{logical_path: record_id}`` of one source (paths only)."""
        with self._reading() as conn:
            if not _has_table(conn, "records"):
                return {}
            rows = conn.execute(
                "SELECT logical_path, record_id FROM records"
                " WHERE source_id = ? AND logical_path IS NOT NULL",
                (source_id,),
            ).fetchall()
        return {row["logical_path"]: row["record_id"] for row in rows}

    # ── scope ────────────────────────────────────────────────────

    def catalog(self, workspace_id: str) -> ScopeCatalog:
        """A consistent snapshot of the workspace's scope metadata."""
        wid = _check(_WORKSPACE_ID, workspace_id, "workspace_id")
        with self._reading() as conn:
            return self._catalog(conn, wid)

    def _catalog(self, conn: sqlite3.Connection, wid: str) -> ScopeCatalog:
        self._workspace(conn, wid)
        projects = frozenset(
            r[0]
            for r in conn.execute("SELECT project_id FROM projects WHERE workspace_id = ?", (wid,))
        )
        sources = frozenset(
            r[0]
            for r in conn.execute("SELECT source_id FROM sources WHERE workspace_id = ?", (wid,))
        )
        collections = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT collection_id, project_id FROM collections WHERE workspace_id = ?", (wid,)
            )
        }
        profiles = {
            r[0]: ScopeProfile.model_validate_json(r[1])
            for r in conn.execute(
                "SELECT profile_id, profile_json FROM scope_profiles WHERE workspace_id = ?", (wid,)
            )
        }
        return ScopeCatalog(wid, projects, sources, collections, profiles)

    def resolve(
        self, context: QueryContext, *, configured_profile: str = DEFAULT_PROFILE_ID
    ) -> EffectiveScope:
        """Validate ``context`` and resolve it to the concrete scope (typed errors)."""
        with self._reading() as conn:
            return resolve_scope(
                context,
                self._catalog(conn, context.workspace_id),
                configured_profile=configured_profile,
            )

    def list_records(
        self, scope: EffectiveScope, *, limit: int = 100, offset: int = 0
    ) -> tuple[list[SourceRecord], int]:
        """The records in ``scope`` (ordered by identity) and the total count."""
        if not 1 <= limit <= MAX_PAGE:
            raise InvalidInputError(f"limit must be between 1 and {MAX_PAGE}")
        if offset < 0:
            raise InvalidInputError("offset must not be negative")
        if scope.empty or not scope.source_ids:
            return [], 0
        where, params = _scope_filter(scope)
        with self._reading() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM records r JOIN sources s USING (source_id) WHERE "
                    + where,
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT r.*, s.workspace_id FROM records r JOIN sources s USING (source_id)"
                " WHERE " + where + " ORDER BY r.source_id, r.record_id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return self._records_with_labels(conn, rows), total

    def query_records(
        self,
        context: QueryContext,
        *,
        configured_profile: str = DEFAULT_PROFILE_ID,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[EffectiveScope, list[SourceRecord], int]:
        """Resolve ``context`` and list its records from one consistent snapshot."""
        with self._reading():
            scope = self.resolve(context, configured_profile=configured_profile)
            records, total = self.list_records(scope, limit=limit, offset=offset)
        return scope, records, total

    def _records_with_labels(
        self, conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> list[SourceRecord]:
        tags: dict[tuple[str, str], list[str]] = {}
        members: dict[tuple[str, str], list[str]] = {}
        for chunk in _chunks(rows, 200):
            keys = [(row["source_id"], row["record_id"]) for row in chunk]
            clause = " OR ".join("(source_id = ? AND record_id = ?)" for _ in keys)
            flat = [part for key in keys for part in key]
            for t in conn.execute(
                f"SELECT source_id, record_id, tag FROM record_tags WHERE {clause}", flat
            ):
                tags.setdefault((t[0], t[1]), []).append(t[2])
            for m in conn.execute(
                f"SELECT source_id, record_id, collection_id FROM collection_members WHERE {clause}",
                flat,
            ):
                members.setdefault((m[0], m[1]), []).append(m[2])
        aliases: dict[tuple[str, str], list[str]] = {}
        if rows and _has_table(conn, "record_aliases"):
            for chunk in _chunks(rows, 200):
                keys = [(row["source_id"], row["record_id"]) for row in chunk]
                clause = " OR ".join("(source_id = ? AND record_id = ?)" for _ in keys)
                flat = [part for key in keys for part in key]
                for a in conn.execute(
                    f"SELECT source_id, record_id, alias FROM record_aliases WHERE {clause}"
                    " ORDER BY alias",
                    flat,
                ):
                    aliases.setdefault((a[0], a[1]), []).append(a[2])
        out: list[SourceRecord] = []
        for row in rows:
            key = (row["source_id"], row["record_id"])
            columns = set(row.keys())
            extra: dict[str, Any] = {}
            if "retention" in columns:  # schema >= 2 (evidence store)
                extra["rights"] = RightsInfo(
                    license=row["license"],
                    retention=RetentionMode(row["retention"]),
                    notes=row["rights_notes"],
                )
                extra["bibliographic"] = Bibliographic.model_validate_json(
                    row["bibliographic_json"] or "{}"
                )
            out.append(
                SourceRecord(
                    source_id=row["source_id"],
                    record_id=row["record_id"],
                    scope=RecordScope(
                        workspace_id=row["workspace_id"], project_id=row["project_id"]
                    ),
                    canonical_uri=row["canonical_uri"],
                    logical_path=row["logical_path"],
                    title=row["title"],
                    research_type=ResearchType(row["research_type"]),
                    curation_status=CurationStatus(row["curation_status"]),
                    tags=tuple(tags.get(key, ())),
                    collection_ids=tuple(members.get(key, ())),
                    aliases=tuple(aliases.get(key, ())),
                    **extra,
                )
            )
        return out

    def records_with_labels(
        self, conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> list[SourceRecord]:
        """:class:`SourceRecord` models of ``records`` rows joined with ``workspace_id``."""
        return self._records_with_labels(conn, rows)

    def record_row(self, conn: sqlite3.Connection, source_id: str, record_id: str) -> sqlite3.Row:
        """The ``records`` row (plus ``workspace_id``) of a record, or ``unknown_scope``."""
        return self._record_row(conn, RecordRef.parse((source_id, record_id)))

    # ── profiles ─────────────────────────────────────────────────

    def set_profile(self, profile: ScopeProfile) -> ScopeProfile:
        """Create or replace a named profile after validating every id it names."""

        def run() -> ScopeProfile:
            with self._writing() as conn:
                catalog = self._catalog(conn, profile.workspace_id)
                for kind, ids, known in (
                    ("source", profile.source_ids, catalog.sources),
                    ("collection", profile.collection_ids, set(catalog.collections)),
                ):
                    missing = sorted(set(ids or ()) - set(known))
                    if missing:
                        raise UnknownScopeError(
                            f"unknown {kind} id(s): {', '.join(missing)}",
                            details={"kind": kind, "ids": missing},
                        )
                conn.execute(
                    "INSERT INTO scope_profiles (workspace_id, profile_id, profile_json, updated_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT (workspace_id, profile_id) DO UPDATE SET"
                    " profile_json = excluded.profile_json, updated_at = excluded.updated_at",
                    (profile.workspace_id, profile.profile_id, profile.model_dump_json(), _now()),
                )
                return profile

        return self._guard(run)

    def list_profiles(self, workspace_id: str) -> list[ScopeProfile]:
        """Stored profiles of a workspace (the built-in ``default`` is listed when not stored)."""
        catalog = self.catalog(workspace_id)
        profiles = dict(catalog.profiles)
        if DEFAULT_PROFILE_ID not in profiles:
            profiles[DEFAULT_PROFILE_ID] = ScopeProfile(
                workspace_id=catalog.workspace_id, profile_id=DEFAULT_PROFILE_ID
            )
        return [profiles[name] for name in sorted(profiles)]

    # ── status ───────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Counts per object kind and per classification value, plus the schema version."""
        with self._reading() as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("workspaces", "projects", "sources", "collections", "records")
            }
            counts["collection_memberships"] = int(
                conn.execute("SELECT COUNT(*) FROM collection_members").fetchone()[0]
            )
            by_type = {
                r[0]: int(r[1])
                for r in conn.execute(
                    "SELECT research_type, COUNT(*) FROM records GROUP BY research_type"
                )
            }
            by_status = {
                r[0]: int(r[1])
                for r in conn.execute(
                    "SELECT curation_status, COUNT(*) FROM records GROUP BY curation_status"
                )
            }
            last = conn.execute(
                "SELECT run_id, source_id, status, processed, total, updated_at"
                " FROM backfill_runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
            version = read_schema_version(conn)
        return {
            "schema_version": version,
            "latest_schema_version": latest_schema_version(),
            "counts": counts,
            "records_by_research_type": dict(sorted(by_type.items())),
            "records_by_curation_status": dict(sorted(by_status.items())),
            "last_backfill": dict(last) if last is not None else None,
        }

    # ── backfill checkpoints ─────────────────────────────────────

    def open_run(self, source_id: str) -> dict[str, Any] | None:
        """The unfinished (running or failed) backfill run of ``source_id``, if any."""
        with self._reading() as conn:
            if not _has_table(conn, "backfill_runs"):
                return None
            row = conn.execute(
                "SELECT * FROM backfill_runs WHERE source_id = ? AND status != 'completed'"
                " ORDER BY started_at DESC, run_id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def start_run(self, source_id: str, total: int, backup_path: str | None) -> dict[str, Any]:
        run_id = "bf-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)

        def run() -> dict[str, Any]:
            with self._writing() as conn:
                now = _now()
                conn.execute(
                    "INSERT INTO backfill_runs (run_id, source_id, status, started_at, updated_at,"
                    " total, backup_path) VALUES (?, ?, 'running', ?, ?, ?, ?)",
                    (run_id, source_id, now, now, total, backup_path),
                )
                return dict(
                    conn.execute(
                        "SELECT * FROM backfill_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                )

        return self._guard(run)

    def resume_run(self, run_id: str, total: int, backup_path: str | None) -> None:
        def run() -> None:
            with self._writing() as conn:
                conn.execute(
                    "UPDATE backfill_runs SET status = 'running', error = NULL, total = ?,"
                    " backup_path = COALESCE(?, backup_path), updated_at = ? WHERE run_id = ?",
                    (total, backup_path, _now(), run_id),
                )

        self._guard(run)

    def commit_batch(
        self, run_id: str, source_id: str, batch: Sequence[NewRecord], last_key: str
    ) -> tuple[int, int]:
        """Insert one batch and advance the run's checkpoint in the same transaction."""

        def run() -> tuple[int, int]:
            with self._writing() as conn:
                created, existing = self._insert_records(conn, source_id, batch)
                conn.execute(
                    "UPDATE backfill_runs SET last_key = ?, processed = processed + ?,"
                    " created = created + ?, existing = existing + ?, batches = batches + 1,"
                    " updated_at = ? WHERE run_id = ?",
                    (last_key, len(batch), created, existing, _now(), run_id),
                )
                return created, existing

        return self._guard(run)

    def finish_run(self, run_id: str, *, error: str | None = None) -> dict[str, Any]:
        status = "failed" if error else "completed"

        def run() -> dict[str, Any]:
            with self._writing() as conn:
                now = _now()
                conn.execute(
                    "UPDATE backfill_runs SET status = ?, error = ?, updated_at = ?,"
                    " finished_at = CASE WHEN ? = 'completed' THEN ? ELSE finished_at END"
                    " WHERE run_id = ?",
                    (status, error, now, status, now, run_id),
                )
                return dict(
                    conn.execute(
                        "SELECT * FROM backfill_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                )

        return self._guard(run)

    # ── source configuration and search snapshots ────────────────

    def source_config(self, source_id: str) -> dict[str, Any]:
        """The stored JSON configuration of a source (``{}`` when none)."""
        sid = _check(_SOURCE_ID, source_id, "source_id")
        with self._reading() as conn:
            self._source(conn, sid)
            row = conn.execute(
                "SELECT config_json FROM sources WHERE source_id = ?", (sid,)
            ).fetchone()
        return _load_config(row["config_json"], sid)

    def set_source_config(self, source_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Replace a source's JSON configuration (validated as a bounded JSON object)."""
        sid = _check(_SOURCE_ID, source_id, "source_id")
        if not isinstance(config, dict):
            raise InvalidInputError("a source configuration is a JSON object")
        text = json.dumps(config, sort_keys=True)
        if len(text.encode("utf-8")) > MAX_SOURCE_CONFIG_BYTES:
            raise InvalidInputError(
                "source configuration too large", details={"max_bytes": MAX_SOURCE_CONFIG_BYTES}
            )

        def run() -> dict[str, Any]:
            with self._writing() as conn:
                self._source(conn, sid)
                conn.execute(
                    "UPDATE sources SET config_json = ?, updated_at = ? WHERE source_id = ?",
                    (text, _now(), sid),
                )
            return dict(config)

        return self._guard(run)

    def snapshot(
        self, context: QueryContext, *, configured_profile: str = DEFAULT_PROFILE_ID
    ) -> ScopeSnapshot:
        """Resolve ``context`` and load its sources and in-scope records in one transaction.

        Every id is validated (typed errors as :meth:`resolve`). The record
        metadata (project, research type, tags, collection membership, logical
        path, title) of every in-scope record is loaded, at most
        :data:`MAX_SNAPSHOT_RECORDS`; a larger scope is ``invalid_scope``.
        """

        def run() -> ScopeSnapshot:
            with self._reading() as conn:
                catalog = self._catalog(conn, context.workspace_id)
                scope = resolve_scope(context, catalog, configured_profile=configured_profile)
                sources = {
                    row["source_id"]: SnapshotSource(
                        source_id=row["source_id"],
                        kind=row["kind"],
                        display_name=row["display_name"],
                        config=_load_config(row["config_json"], row["source_id"]),
                    )
                    for row in conn.execute(
                        "SELECT source_id, kind, display_name, config_json FROM sources"
                        " WHERE workspace_id = ? ORDER BY source_id",
                        (catalog.workspace_id,),
                    )
                }
                record_sources = frozenset(
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT r.source_id FROM records r JOIN sources s"
                        " USING (source_id) WHERE s.workspace_id = ?",
                        (catalog.workspace_id,),
                    )
                )
                return ScopeSnapshot(
                    scope, sources, self._snapshot_records(conn, scope), record_sources
                )

        return self._guard(run)

    def _snapshot_records(
        self, conn: sqlite3.Connection, scope: EffectiveScope
    ) -> dict[str, dict[str, SnapshotRecord]]:
        if scope.empty or not scope.source_ids:
            return {}
        where, params = _scope_filter(scope)
        base = "FROM records r JOIN sources s USING (source_id) WHERE " + where
        count = int(conn.execute("SELECT COUNT(*) " + base, params).fetchone()[0])
        if count > MAX_SNAPSHOT_RECORDS:
            raise InvalidScopeError(
                f"the scope holds {count} records, more than one search can load"
                f" ({MAX_SNAPSHOT_RECORDS}); narrow it by project, collection, type or tag",
                details={"records": count, "max_records": MAX_SNAPSHOT_RECORDS},
            )
        tags: dict[tuple[str, str], set[str]] = {}
        for row in conn.execute(
            "SELECT t.source_id, t.record_id, t.tag FROM record_tags t JOIN records r"
            " ON r.source_id = t.source_id AND r.record_id = t.record_id"
            " JOIN sources s ON s.source_id = r.source_id WHERE " + where,
            params,
        ):
            tags.setdefault((row[0], row[1]), set()).add(row[2])
        members: dict[tuple[str, str], set[str]] = {}
        for row in conn.execute(
            "SELECT m.source_id, m.record_id, m.collection_id FROM collection_members m"
            " JOIN records r ON r.source_id = m.source_id AND r.record_id = m.record_id"
            " JOIN sources s ON s.source_id = r.source_id WHERE " + where,
            params,
        ):
            members.setdefault((row[0], row[1]), set()).add(row[2])
        out: dict[str, dict[str, SnapshotRecord]] = {}
        for row in conn.execute(
            "SELECT r.source_id, r.record_id, r.project_id, r.research_type, r.logical_path,"
            " r.title " + base,
            params,
        ):
            key = (row["source_id"], row["record_id"])
            view = RecordView(
                source_id=row["source_id"],
                record_id=row["record_id"],
                project_id=row["project_id"],
                research_type=ResearchType(row["research_type"]),
                tags=frozenset(tags.get(key, ())),
                collection_ids=frozenset(members.get(key, ())),
            )
            out.setdefault(row["source_id"], {})[row["record_id"]] = SnapshotRecord(
                view=view, logical_path=row["logical_path"], title=row["title"]
            )
        return out

    def count_records(self, source_id: str) -> int:
        with self._reading() as conn:
            if not _has_table(conn, "records"):
                return 0
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM records WHERE source_id = ?", (source_id,)
                ).fetchone()[0]
            )


# ── helpers ──────────────────────────────────────────────────────────


def _refuse_newer(conn: sqlite3.Connection, path: Path) -> None:
    current = read_schema_version(conn)
    latest = latest_schema_version()
    if current > latest:
        raise IncompatibleError(
            f"knowledge store schema {current} is newer than this engine supports ({latest});"
            " upgrade the engine or restore the backup taken before the upgrade",
            details={"schema_version": current, "supported": latest, "store": path.name},
        )


def _exists(conn: sqlite3.Connection, table: str, column: str, value: str) -> bool:
    return (
        conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)).fetchone() is not None
    )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _parse_refs(refs: Iterable[Any]) -> list[RecordRef]:
    parsed = list(dict.fromkeys(RecordRef.parse(ref) for ref in refs))
    if not parsed:
        raise InvalidInputError("at least one record reference is required")
    if len(parsed) > MAX_PAGE:
        raise InvalidInputError(f"at most {MAX_PAGE} records per call")
    return parsed


def _chunks[V](items: Sequence[V], size: int) -> Iterator[Sequence[V]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(values: Sequence[Any]) -> str:
    if len(values) > MAX_SELECTION * 2:  # pragma: no cover - bounded by the contract
        raise InvalidInputError("selection too large")
    return ", ".join("?" for _ in values)


def _scope_filter(scope: EffectiveScope) -> tuple[str, list[Any]]:
    """SQL ``WHERE`` text equivalent to :func:`~knowledge_engine.knowledge.scope.record_in_scope`."""
    clauses = ["s.workspace_id = ?", f"r.source_id IN ({_placeholders(scope.source_ids)})"]
    params: list[Any] = [scope.workspace_id, *scope.source_ids]
    if scope.project_id is not None:
        if scope.include_workspace:
            clauses.append("(r.project_id = ? OR r.project_id IS NULL)")
        else:
            clauses.append("r.project_id = ?")
        params.append(scope.project_id)
    if scope.collection_ids is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM collection_members m WHERE m.source_id = r.source_id"
            f" AND m.record_id = r.record_id AND m.collection_id IN"
            f" ({_placeholders(scope.collection_ids)}))"
        )
        params.extend(scope.collection_ids)
    if scope.research_types is not None:
        clauses.append(f"r.research_type IN ({_placeholders(scope.research_types)})")
        params.extend(t.value for t in scope.research_types)
    if scope.tags:
        clauses.append(
            "(SELECT COUNT(DISTINCT t.tag) FROM record_tags t WHERE t.source_id = r.source_id"
            f" AND t.record_id = r.record_id AND t.tag IN ({_placeholders(scope.tags)})) = ?"
        )
        params.extend([*scope.tags, len(scope.tags)])
    return " AND ".join(clauses), params


def _load_config(text: str | None, source_id: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        _logger.warning("source %s has an unreadable configuration; ignoring it", source_id)
        return {}
    return value if isinstance(value, dict) else {}


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _workspace_model(row: sqlite3.Row) -> Workspace:
    return Workspace(
        workspace_id=row["workspace_id"],
        display_name=row["display_name"],
        description=row["description"],
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


def _project_model(row: sqlite3.Row) -> Project:
    return Project(
        project_id=row["project_id"],
        workspace_id=row["workspace_id"],
        display_name=row["display_name"],
        description=row["description"],
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


def _source_model(row: sqlite3.Row) -> Source:
    return Source(
        source_id=row["source_id"],
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        display_name=row["display_name"],
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


def _collection_model(row: sqlite3.Row) -> Collection:
    return Collection(
        collection_id=row["collection_id"],
        workspace_id=row["workspace_id"],
        project_id=row["project_id"],
        display_name=row["display_name"],
        description=row["description"],
        member_count=int(row["member_count"]),
        created_at=_ts(row["created_at"]),
        updated_at=_ts(row["updated_at"]),
    )


__all__ = [
    "KNOWLEDGE_DB_FILENAME",
    "MAX_PAGE",
    "MAX_SNAPSHOT_RECORDS",
    "MAX_SOURCE_CONFIG_BYTES",
    "MIGRATIONS_DIR",
    "SOURCE_KINDS",
    "UNSET",
    "KnowledgeStore",
    "NewRecord",
    "RecordRef",
    "ScopeSnapshot",
    "SnapshotRecord",
    "SnapshotSource",
    "latest_schema_version",
]
