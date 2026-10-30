"""The broker source ``serena:<project_id>``: code symbols as hits, files as evidence.

Each configured project whose ``project_id`` exists in ``knowledge.db`` gets a
source of kind ``serena`` in its workspace (:func:`register_sources`; source
configuration ``{"project_id": ...}``). The adapter:

* **searches only when explicitly selected**: a query whose
  ``context.source_ids`` names the source. A workspace-wide query that takes
  its sources from the profile gets an empty, healthy answer from it, so code
  symbols never leak into ordinary knowledge search and Serena is never
  started by one. Symbol search is lexical: an explicit selection with mode
  ``vector``/``hybrid`` fails ``unavailable``.
* **scopes like project context**: the project's records are records of that
  project with research type ``other`` and no tags or collections, decided by
  :func:`~knowledge_engine.knowledge.scope.record_in_scope`.
* **maps symbols to files**: a hit is the repository file holding the symbol
  (record id :func:`~.tools.record_id_for`, revision id content-addressed over
  the file's current bytes) with a code locator for the symbol pinned to the
  commit or the dirty snapshot digest; the excerpt is the symbol's lines.
* **opens exact revisions**: the current file bytes when they still have the
  requested revision id (``verified``; the locator's lines when given), else a
  revision recorded earlier in the evidence store (``stale``: the file no
  longer holds it), else ``unsupported``/``unavailable`` -- never newer content.
* **records what it opens**: a verified open is ingested into the evidence
  store (same ``knowledge.db``; upstream version = commit or snapshot digest)
  and assigned to the project, so the citation service can validate and
  export code evidence against that exact revision later.

Serena memories are not imported (ADR 0016).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from ..knowledge.adapter import AdapterCapabilities, AdapterSearchResult
from ..knowledge.contracts import (
    CodeLocator,
    EffectiveScope,
    Locator,
    OpenResult,
    QueryContext,
    RankingInfo,
    RecordScope,
    ResearchType,
    SearchHit,
    SourceState,
    SourceStatus,
    UnsupportedLocator,
    VerificationState,
)
from ..knowledge.errors import (
    InvalidInputError,
    KnowledgeError,
    UnavailableError,
    UnknownScopeError,
)
from ..knowledge.scope import RecordView, record_in_scope
from ..knowledge.sources._common import MAX_EXCERPT_CHARS, clip, revision_of
from .manager import SerenaManager
from .repo import RepoState, read_file
from .tools import FindSymbolArgs, normalize, path_for

if TYPE_CHECKING:
    from ..core.settings import Settings
    from ..knowledge.evidence.store import EvidenceStore
    from ..knowledge.store import KnowledgeStore, ScopeSnapshot
    from .config import SerenaConfig

_logger = logging.getLogger(__name__)

SOURCE_KIND = "serena"
SOURCE_NAMESPACE = "serena"
MAX_QUERY_CHARS = 500
_LOCATOR = TypeAdapter(Locator)
ManagerGetter = Callable[[], SerenaManager]


def source_id_for(project_id: str) -> str:
    """The broker source id of a configured project."""
    return f"{SOURCE_NAMESPACE}:{project_id}"


def media_type_for(path: str) -> str:
    """``text/x-python`` for ``.py``/``.pyi``, ``text/plain`` otherwise."""
    return "text/x-python" if path.endswith((".py", ".pyi")) else "text/plain"


def register_sources(store: KnowledgeStore, config: SerenaConfig) -> dict[str, Any]:
    """Register a ``serena`` source per configured project (idempotent)."""
    registered: list[str] = []
    skipped: list[dict[str, str]] = []
    for project_id in sorted(config.projects):
        source_id = source_id_for(project_id)
        try:
            project = store.get_project(project_id)
            store.register_source(
                project.workspace_id,
                source_id,
                kind=SOURCE_KIND,
                display_name=f"Code symbols ({project_id})"[:200],
                config={"project_id": project_id},
            )
            registered.append(source_id)
        except KnowledgeError as exc:
            _logger.warning("Serena source for %s not registered: %s", project_id, exc.message)
            skipped.append({"project_id": project_id, "reason": exc.message})
    return {"registered": registered, "skipped": skipped}


def manager_getter(settings: Settings) -> ManagerGetter | None:
    """``default_manager`` when the adapter is enabled, else ``None``."""
    if not settings.serena_enabled:
        return None
    from .manager import default_manager  # noqa: PLC0415

    return default_manager


def _lines_of(data: bytes, locator: CodeLocator) -> bytes | None:
    if locator.start_line is None or locator.end_line is None:
        return None
    lines = data.splitlines(keepends=True)
    if locator.end_line > len(lines) + 1:
        return None
    return b"".join(lines[locator.start_line - 1 : locator.end_line - 1])


class SerenaSourceAdapter:
    """Search/open of one project's code symbols through its Serena session."""

    def __init__(
        self,
        source_id: str,
        snapshot: ScopeSnapshot,
        manager: ManagerGetter,
        *,
        evidence: EvidenceStore | None = None,
        profile: str = "default",
    ) -> None:
        self._source_id = source_id
        source = snapshot.sources.get(source_id)
        configured = source.config.get("project_id") if source is not None else None
        self._project_id = (
            configured if isinstance(configured, str) else source_id.partition(":")[2]
        )
        self._manager = manager
        self._evidence = evidence
        self._profile = profile
        self.warnings: list[str] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    def describe_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source_id=self._source_id,
            source_kind=SOURCE_KIND,
            # Every mode is accepted so an unselected source stays silent in
            # vector/hybrid queries; an explicit selection must use ``fts``.
            modes=("fts", "vector", "hybrid"),
            filters=("project", "collection", "research_type", "tag"),
            locator_kinds=("code", "unsupported"),
            supports_open=True,
            max_limit=50,
        )

    def health(self) -> SourceStatus:
        return SourceStatus(source_id=self._source_id, state=SourceState.OK)

    def _visible(self, scope: EffectiveScope) -> bool:
        if scope.empty or self._source_id not in scope.source_ids:
            return False
        view = RecordView(
            source_id=self._source_id,
            record_id=self._project_id,
            project_id=self._project_id,
            research_type=ResearchType.OTHER,
        )
        return record_in_scope(view, scope)

    def _project(self) -> tuple[SerenaManager, Any]:
        manager = self._manager()
        try:
            return manager, manager.project(self._project_id)
        except InvalidInputError as exc:
            raise UnavailableError(
                f"project {self._project_id!r} is no longer configured for Serena",
                details={"project_id": self._project_id},
            ) from exc

    # ── search ───────────────────────────────────────────────────

    def _empty(self, started: float) -> AdapterSearchResult:
        status = SourceStatus(
            source_id=self._source_id,
            state=SourceState.OK,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return AdapterSearchResult(source_id=self._source_id, status=status)

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        started = time.monotonic()
        explicit = context.source_ids is not None and self._source_id in context.source_ids
        if not explicit or not self._visible(scope):
            return self._empty(started)
        if context.mode != "fts":
            raise UnavailableError(
                "code symbol search is lexical; select this source with mode 'fts'",
                details={"source_id": self._source_id, "mode": context.mode},
            )
        pattern = query.strip()
        if not pattern or len(pattern) > MAX_QUERY_CHARS or "\n" in pattern:
            raise InvalidInputError(
                f"a symbol query is one line of at most {MAX_QUERY_CHARS} characters"
            )
        manager, project = self._project()
        args = FindSymbolArgs(
            project_id=self._project_id,
            name_path_pattern=pattern,
            substring_matching=True,
            max_matches=context.limit,
        )
        payload = manager.call(
            self._project_id,
            "find_symbol",
            {
                "name_path_pattern": pattern,
                "substring_matching": True,
                "include_body": False,
                "max_matches": context.limit,
            },
        )
        state = manager.repo_states.get(project.root)
        normalized = normalize(
            "find_symbol",
            args,
            payload,
            root=project.root,
            repository_id=project.repository_id,
            state=state,
        )
        hits: list[SearchHit] = []
        files: dict[str, bytes | None] = {}
        for entry in normalized.results:
            path = entry["path"]
            if path not in files:
                files[path] = read_file(project.root, path)
            data = files[path]
            locator = _LOCATOR.validate_python(entry["locator"])
            record_id = entry["record_id"]
            excerpt = b""
            if data is not None and isinstance(locator, CodeLocator):
                excerpt = _lines_of(data, locator) or b""
            hits.append(
                SearchHit(
                    source_id=self._source_id,
                    record_id=record_id,
                    revision_id=(
                        revision_of(self._source_id, record_id, data)[0]
                        if data is not None
                        else None
                    ),
                    scope=RecordScope(workspace_id=scope.workspace_id, project_id=self._project_id),
                    research_type=ResearchType.OTHER,
                    title=clip(f"{entry['name_path']} ({entry['kind']}) in {path}", 1000),
                    locator=locator,
                    excerpt=clip(excerpt.decode("utf-8", errors="replace"), MAX_EXCERPT_CHARS),
                    provenance_id=f"{SOURCE_NAMESPACE}:{project.repository_id}"[:200],
                    ranking=RankingInfo(native_rank=len(hits) + 1, score_kind="other"),
                )
            )
            if len(hits) >= context.limit:
                break
        elapsed = int((time.monotonic() - started) * 1000)
        if normalized.rejected_paths:
            self.warnings.append(
                f"{len(normalized.rejected_paths)} Serena result(s) outside the project dropped"
            )
        status = SourceStatus(
            source_id=self._source_id,
            state=SourceState.OK,
            hit_count=len(hits),
            elapsed_ms=elapsed,
        )
        return AdapterSearchResult(source_id=self._source_id, hits=tuple(hits), status=status)

    # ── open ─────────────────────────────────────────────────────

    def _path(self, record_id: str) -> str:
        path = path_for(record_id)
        if path is None and self._evidence is not None:
            try:
                path = self._evidence.store.get_record(self._source_id, record_id).logical_path
            except KnowledgeError:
                path = None
        if path is None:
            raise UnknownScopeError(
                f"unknown record {self._source_id}/{record_id}",
                details={"kind": "record", "source_id": self._source_id, "id": record_id},
            )
        return path

    def current_revision(self, context: QueryContext, scope: EffectiveScope, record_id: str) -> str:
        if not self._visible(scope):
            raise UnknownScopeError(f"unknown record {self._source_id}/{record_id} in the scope")
        _, project = self._project()
        data = read_file(project.root, self._path(record_id))
        if data is None:
            raise UnavailableError(f"{self._source_id}/{record_id} is not readable")
        return revision_of(self._source_id, record_id, data)[0]

    def open(
        self,
        context: QueryContext,
        scope: EffectiveScope,
        record_id: str,
        revision_id: str,
        locator: Locator | None = None,
    ) -> OpenResult:
        if not self._visible(scope):
            raise UnknownScopeError(
                f"unknown record {self._source_id}/{record_id} in the effective scope",
                details={"kind": "record", "source_id": self._source_id, "id": record_id},
            )
        path = self._path(record_id)
        if locator is not None and (not isinstance(locator, CodeLocator) or locator.path != path):
            raise InvalidInputError(
                "this source serves code locators for the record's own path",
                details={"field": "locator", "path": path},
            )
        _, project = self._project()
        record_scope = RecordScope(workspace_id=scope.workspace_id, project_id=self._project_id)
        common: dict[str, Any] = {
            "source_id": self._source_id,
            "record_id": record_id,
            "revision_id": revision_id,
            "scope": record_scope,
            "media_type": media_type_for(path),
            "provenance_id": f"{SOURCE_NAMESPACE}:{project.repository_id}"[:200],
            "source_status": SourceStatus(source_id=self._source_id, state=SourceState.OK),
        }
        data = read_file(project.root, path)
        current = revision_of(self._source_id, record_id, data) if data is not None else None
        if data is not None and current is not None and current[0] == revision_id:
            state = self._manager().repo_states.get(project.root)
            self._capture(record_id, path, data, state)
            return self._verified(common, data, current[1], locator, state)
        recorded = self._from_evidence(context, record_id, revision_id, locator)
        if recorded is not None:
            if recorded.verification == VerificationState.VERIFIED:
                # The file no longer holds this revision (changed or deleted), even if
                # the evidence store has nothing newer: serve the cited bytes as stale.
                return recorded.model_copy(update={"verification": VerificationState.STALE})
            return recorded
        reason = (
            "the file no longer exists"
            if data is None
            else "the file changed since that revision and the revision was not recorded"
        )
        verification = (
            VerificationState.UNAVAILABLE if data is None else VerificationState.UNSUPPORTED
        )
        return OpenResult(
            **common, verification=verification, locator=UnsupportedLocator(reason=reason)
        )

    def _verified(
        self,
        common: dict[str, Any],
        data: bytes,
        digest: str,
        locator: Locator | None,
        state: RepoState,
    ) -> OpenResult:
        content = data
        if isinstance(locator, CodeLocator):
            pinned = locator.commit or locator.snapshot_digest
            if pinned != state.version:
                return OpenResult(
                    **common,
                    verification=VerificationState.UNSUPPORTED,
                    locator=UnsupportedLocator(
                        reason="the locator's commit/snapshot is not the repository's "
                        "current version"
                    ),
                )
            sliced = _lines_of(data, locator)
            if sliced is None and locator.start_line is not None:
                return OpenResult(
                    **common,
                    verification=VerificationState.UNSUPPORTED,
                    locator=UnsupportedLocator(reason="the locator's lines are outside the file"),
                )
            content = sliced if sliced is not None else data
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return OpenResult(
                **common,
                verification=VerificationState.UNSUPPORTED,
                digest=digest,
                locator=UnsupportedLocator(reason="the file is not valid UTF-8"),
            )
        return OpenResult(
            **common,
            verification=VerificationState.VERIFIED,
            digest=digest,
            content=text,
            locator=locator,
        )

    def _capture(self, record_id: str, path: str, data: bytes, state: RepoState) -> None:
        """Record the opened revision in the evidence store (best effort, logged)."""
        if self._evidence is None:
            return
        from ..knowledge.evidence.store import IngestRequest  # noqa: PLC0415

        try:
            self._evidence.ingest(
                IngestRequest(
                    source_id=self._source_id,
                    record_id=record_id,
                    logical_path=path,
                    title=path,
                    media_type=media_type_for(path),
                    content=data,
                    upstream_version=state.version,
                )
            )
            record = self._evidence.store.get_record(self._source_id, record_id)
            if record.scope.project_id != self._project_id:
                self._evidence.store.classify(
                    self._source_id, record_id, project_id=self._project_id
                )
        except KnowledgeError as exc:
            _logger.warning(
                "could not record %s/%s as evidence: %s", self._source_id, record_id, exc.message
            )

    def _from_evidence(
        self,
        context: QueryContext,
        record_id: str,
        revision_id: str,
        locator: Locator | None,
    ) -> OpenResult | None:
        if self._evidence is None:
            return None
        from ..knowledge.evidence.resolve import open_evidence  # noqa: PLC0415

        try:
            resolution = open_evidence(
                self._evidence,
                context,
                source_id=self._source_id,
                record_id=record_id,
                revision_id=revision_id,
                locator=locator,
                configured_profile=self._profile,
            )
        except UnknownScopeError:
            return None
        return resolution.result


def adapter_factory_entry(
    manager: ManagerGetter | None,
    *,
    evidence: EvidenceStore | None,
    profile: str,
) -> Callable[[str, ScopeSnapshot], SerenaSourceAdapter]:
    """The broker factory branch for ``serena`` sources (``unavailable`` when disabled)."""

    def build(source_id: str, snapshot: ScopeSnapshot) -> SerenaSourceAdapter:
        if manager is None:
            raise UnavailableError(
                "the Serena adapter is disabled (KE_SERENA_ENABLED=0)",
                details={"source_id": source_id},
            )
        return SerenaSourceAdapter(source_id, snapshot, manager, evidence=evidence, profile=profile)

    return build


__all__ = [
    "SOURCE_KIND",
    "SerenaSourceAdapter",
    "adapter_factory_entry",
    "manager_getter",
    "media_type_for",
    "register_sources",
    "source_id_for",
]
