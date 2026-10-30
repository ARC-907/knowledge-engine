"""The project-context source adapter: per-project documentation stores behind one source.

Project-context stores live under each project root
(``<root>/.knowledge-engine/...``) and identify projects by fingerprint
(``proj_...``). They know nothing of ``knowledge.db``. An operator **links** a
scoped-knowledge project (P1 project id) to one fingerprint at one root
(:func:`link_project`); the links are the configuration of the workspace's
source ``project_context:<workspace_id>`` (kind ``project_context``). Only
linked fingerprints are searchable, and each link is searched with its
fingerprint as an SQL filter -- two projects whose display names share a
slug (and so one content DB file) never see each other's documents.

Project-context documents are not classified in ``knowledge.db``: each one
is a workspace record of its linked project with research type ``other``, no
tags and no collections, decided by the same reference predicate
(:func:`~knowledge_engine.knowledge.scope.record_in_scope`) every other source
obeys. A project query therefore never returns another project's documents;
a query constrained by collection or tag returns none of them.

Record ids are ``<project_id>:<document id>``. The revision id of a hit is the
content-addressed id of the stored sanitized body; ``open`` serves it when it
is still current (``verified``), ``unsupported`` otherwise (no revision
history yet), and honours ``[project_context.mcp] allow_full_content``
(``denied`` when off). Stores are opened read-only per call.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...project_context import search as pdsearch
from ...project_context.config import ProjectContextConfig, load_config
from ...project_context.paths import (
    canonical_root,
    fingerprint_db_path,
    project_db_path,
    project_slug,
)
from ...retrieval.fts5 import quote_phrase
from ..adapter import AdapterCapabilities, AdapterSearchResult
from ..contracts import (
    OPAQUE_ID_PATTERN,
    EffectiveScope,
    ErrorInfo,
    Locator,
    OpenResult,
    QueryContext,
    RankingInfo,
    RecordScope,
    ResearchType,
    SearchHit,
    SourceState,
    SourceStatus,
)
from ..errors import (
    DeniedError,
    InvalidInputError,
    InvalidScopeError,
    KnowledgeError,
    UnavailableError,
    UnknownScopeError,
)
from ..scope import RecordView, record_in_scope
from ..store import KnowledgeStore, ScopeSnapshot
from ._common import MAX_EXCERPT_CHARS, clip, open_result, revision_of

_logger = logging.getLogger(__name__)

SOURCE_KIND = "project_context"
SOURCE_NAMESPACE = "project_context"
SOURCE_DISPLAY_NAME = "Project context"
#: The RRF constant used to interleave several linked projects' BM25 rankings.
LINK_RRF_K = 60

_DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_FP_RE = re.compile(r"^proj_[a-z0-9]{1,64}$")
_PROJECT_ID_RE = re.compile(OPAQUE_ID_PATTERN)

ConfigLoader = Callable[[Path], ProjectContextConfig]


def source_id_for(workspace_id: str) -> str:
    """The project-context source id of a workspace."""
    return f"{SOURCE_NAMESPACE}:{workspace_id}"


@dataclass(frozen=True)
class ProjectLink:
    """One scoped-knowledge project linked to a project-context fingerprint at a root."""

    project_id: str
    root: str
    project_fp: str


def parse_links(config: dict[str, Any]) -> dict[str, ProjectLink]:
    """The valid links of a source configuration (malformed entries are logged and skipped)."""
    raw = config.get("links")
    links: dict[str, ProjectLink] = {}
    if not isinstance(raw, dict):
        return links
    for project_id, value in raw.items():
        if (
            isinstance(project_id, str)
            and _PROJECT_ID_RE.fullmatch(project_id)
            and isinstance(value, dict)
            and isinstance(value.get("root"), str)
            and isinstance(value.get("project_fp"), str)
            and _FP_RE.fullmatch(value["project_fp"])
        ):
            links[project_id] = ProjectLink(project_id, value["root"], value["project_fp"])
        else:
            _logger.warning("ignoring a malformed project-context link for %r", project_id)
    return links


def _default_loader(root: Path) -> ProjectContextConfig:
    return load_config(start=root)


@contextmanager
def _readonly(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise UnavailableError("a linked project-context store does not exist")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _content_db(link: ProjectLink, cfg: ProjectContextConfig) -> Path:
    """The content DB of ``link`` (its slug comes from the fingerprint registry)."""
    root = Path(link.root)
    with _readonly(fingerprint_db_path(root, cfg)) as registry:
        row = registry.execute(
            "SELECT name FROM projects WHERE project_fp = ?", (link.project_fp,)
        ).fetchone()
    if row is None:
        raise UnavailableError(
            f"project fingerprint {link.project_fp} is no longer registered at its root",
            details={"project_id": link.project_id, "project_fp": link.project_fp},
        )
    return project_db_path(root, cfg, project_slug(str(row["name"])))


def _is_syntax_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "syntax error" in message or "unterminated string" in message or "fts5" in message


class ProjectContextAdapter:
    """Search/open of the project-context documents of a workspace's linked projects."""

    def __init__(
        self,
        source_id: str,
        snapshot: ScopeSnapshot,
        *,
        config_loader: ConfigLoader = _default_loader,
    ) -> None:
        self._source_id = source_id
        source = snapshot.sources.get(source_id)
        self._links = parse_links(source.config if source is not None else {})
        self._load_config = config_loader
        self.warnings: list[str] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    def describe_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source_id=self._source_id,
            source_kind=SOURCE_KIND,
            modes=("fts",),
            filters=("project", "collection", "research_type", "tag"),
            locator_kinds=("text", "unsupported"),
            supports_open=True,
        )

    def health(self) -> SourceStatus:
        return SourceStatus(source_id=self._source_id, state=SourceState.OK)

    def _visible_links(self, scope: EffectiveScope) -> list[ProjectLink]:
        if scope.empty or self._source_id not in scope.source_ids:
            return []
        visible = []
        for project_id in sorted(self._links):
            view = RecordView(
                source_id=self._source_id,
                record_id=project_id,
                project_id=project_id,
                research_type=ResearchType.OTHER,
            )
            if record_in_scope(view, scope):
                visible.append(self._links[project_id])
        return visible

    # ── search ───────────────────────────────────────────────────

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        started = time.monotonic()
        links = self._visible_links(scope)
        rankings: list[list[SearchHit]] = []
        failures: list[KnowledgeError] = []
        for link in links:
            try:
                rankings.append(self._search_link(link, scope, query, context.limit))
            except KnowledgeError as exc:
                failures.append(exc)
            except sqlite3.Error as exc:
                _logger.warning("project-context store of %s failed: %s", link.project_id, exc)
                failures.append(UnavailableError("a linked project-context store failed"))
        if links and len(failures) == len(links):
            raise UnavailableError(
                "no linked project-context store could be searched",
                details={"projects": [link.project_id for link in links]},
            )
        hits = self._interleave(rankings, context.limit)
        elapsed = int((time.monotonic() - started) * 1000)
        if failures:
            self.warnings.append(f"{len(failures)} linked project store(s) failed")
            status = SourceStatus(
                source_id=self._source_id,
                state=SourceState.DEGRADED,
                error=ErrorInfo.model_validate(failures[0].to_dict()),
                hit_count=len(hits),
                elapsed_ms=elapsed,
            )
        else:
            status = SourceStatus(
                source_id=self._source_id,
                state=SourceState.OK,
                hit_count=len(hits),
                elapsed_ms=elapsed,
            )
        return AdapterSearchResult(source_id=self._source_id, hits=tuple(hits), status=status)

    def _search_link(
        self, link: ProjectLink, scope: EffectiveScope, query: str, limit: int
    ) -> list[SearchHit]:
        cfg = self._load_config(Path(link.root))
        with _readonly(_content_db(link, cfg)) as conn:
            try:
                rows = pdsearch.search(conn, query, limit=limit, project_fp=link.project_fp)
            except sqlite3.OperationalError as exc:
                if not _is_syntax_error(exc):
                    raise
                self.warnings.append("the query is not valid FTS5 syntax; searched as a phrase")
                rows = pdsearch.search(
                    conn, quote_phrase(query), limit=limit, project_fp=link.project_fp
                )
            hits: list[SearchHit] = []
            for rank, row in enumerate(rows, start=1):
                doc_id = str(row["record_id"])
                if not _DOC_ID_RE.fullmatch(doc_id):
                    _logger.warning("skipping a project-context document with an odd id")
                    continue
                body = self._body(conn, doc_id)
                record_id = f"{link.project_id}:{doc_id}"
                hits.append(
                    SearchHit(
                        source_id=self._source_id,
                        record_id=record_id,
                        revision_id=(
                            revision_of(self._source_id, record_id, body.encode("utf-8"))[0]
                            if body is not None
                            else None
                        ),
                        scope=RecordScope(
                            workspace_id=scope.workspace_id, project_id=link.project_id
                        ),
                        research_type=ResearchType.OTHER,
                        title=clip(str(row["source_path"] or row["summary"] or doc_id), 1000),
                        excerpt=clip(str(row["snippet"] or ""), MAX_EXCERPT_CHARS),
                        provenance_id=f"{SOURCE_NAMESPACE}:{link.project_fp}",
                        ranking=RankingInfo(
                            native_rank=rank, native_score=float(row["score"]), score_kind="bm25"
                        ),
                    )
                )
        return hits

    @staticmethod
    def _body(conn: sqlite3.Connection, doc_id: str) -> str | None:
        row = conn.execute(
            "SELECT searchable_body FROM project_doc_bodies WHERE record_id = ?", (doc_id,)
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    @staticmethod
    def _interleave(rankings: list[list[SearchHit]], limit: int) -> list[SearchHit]:
        """One link's ranking as is; several links fused by rank (never by raw BM25)."""
        if len(rankings) <= 1:
            return (rankings[0] if rankings else [])[:limit]
        scored: list[tuple[float, str, SearchHit]] = []
        for ranking in rankings:
            for position, hit in enumerate(ranking, start=1):
                scored.append((1.0 / (LINK_RRF_K + position), hit.record_id, hit))
        scored.sort(key=lambda item: (-item[0], item[1]))
        out = []
        for rank, (score, _, hit) in enumerate(scored[:limit], start=1):
            info = RankingInfo(native_rank=rank, native_score=score, score_kind="rrf")
            out.append(hit.model_copy(update={"ranking": info}))
        return out

    # ── open ─────────────────────────────────────────────────────

    def _resolve(self, record_id: str, scope: EffectiveScope) -> tuple[ProjectLink, str]:
        project_id, _, doc_id = record_id.partition(":")
        link = self._links.get(project_id)
        if not doc_id or link is None or link not in self._visible_links(scope):
            raise UnknownScopeError(
                f"unknown record {self._source_id}/{record_id} in the effective scope",
                details={"kind": "record", "source_id": self._source_id, "id": record_id},
            )
        return link, doc_id

    def _document(self, link: ProjectLink, doc_id: str, record_id: str) -> tuple[Any, bool]:
        cfg = self._load_config(Path(link.root))
        with _readonly(_content_db(link, cfg)) as conn:
            row = conn.execute(
                "SELECT pd.record_id, pd.source_path, b.searchable_body FROM project_docs pd"
                " LEFT JOIN project_doc_bodies b ON b.record_id = pd.record_id"
                " WHERE pd.record_id = ? AND pd.project_fp = ?",
                (doc_id, link.project_fp),
            ).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown record {self._source_id}/{record_id} in the effective scope",
                details={"kind": "record", "source_id": self._source_id, "id": record_id},
            )
        return row, bool(cfg.mcp.allow_full_content)

    def current_revision(self, context: QueryContext, scope: EffectiveScope, record_id: str) -> str:
        link, doc_id = self._resolve(record_id, scope)
        row, _ = self._document(link, doc_id, record_id)
        if row["searchable_body"] is None:
            raise UnavailableError(f"the content of {self._source_id}/{record_id} is not stored")
        return revision_of(self._source_id, record_id, str(row["searchable_body"]).encode())[0]

    def open(
        self,
        context: QueryContext,
        scope: EffectiveScope,
        record_id: str,
        revision_id: str,
        locator: Locator | None = None,
    ) -> OpenResult:
        link, doc_id = self._resolve(record_id, scope)
        row, allowed = self._document(link, doc_id, record_id)
        if not allowed:
            raise DeniedError(
                "full project-context content is disabled "
                "([project_context.mcp] allow_full_content = false)"
            )
        body = row["searchable_body"]
        return open_result(
            source_id=self._source_id,
            record_id=record_id,
            requested=revision_id,
            scope=RecordScope(workspace_id=scope.workspace_id, project_id=link.project_id),
            media_type="text/plain",
            data=None if body is None else str(body).encode("utf-8"),
            locator=locator,
            provenance_id=f"{SOURCE_NAMESPACE}:{link.project_fp}",
            missing_reason="the document body is not stored",
        )


# ── link administration ──────────────────────────────────────────────


def _registered_fp(root: Path, cfg: ProjectContextConfig, project_fp: str | None) -> str:
    path = fingerprint_db_path(root, cfg)
    if not path.is_file():
        raise UnavailableError(
            "no project-context registry exists at that root; register the project first",
            details={"hint": "knowledge-engine project-context register"},
        )
    with _readonly(path) as registry:
        if project_fp is None:
            row = registry.execute(
                "SELECT project_fp FROM projects WHERE root_path = ? ORDER BY created_at LIMIT 1",
                (canonical_root(root),),
            ).fetchone()
        else:
            row = registry.execute(
                "SELECT project_fp FROM projects WHERE project_fp = ?", (project_fp,)
            ).fetchone()
    if row is None:
        raise UnknownScopeError(
            "the project fingerprint is not registered at that root",
            details={"kind": "project_fp", "id": project_fp},
        )
    return str(row["project_fp"])


def link_project(
    store: KnowledgeStore,
    workspace_id: str,
    project_id: str,
    root: str,
    *,
    project_fp: str | None = None,
    config_loader: ConfigLoader = _default_loader,
) -> dict[str, Any]:
    """Link ``project_id`` to the project-context project at ``root``.

    Registers the workspace's project-context source when needed. The
    fingerprint defaults to the one registered for ``root``. A fingerprint can
    be linked to one project of the workspace only.
    """
    project = store.get_project(project_id)
    if project.workspace_id != workspace_id:
        raise InvalidScopeError(
            f"project {project_id!r} belongs to another workspace",
            details={"project_id": project_id, "workspace_id": workspace_id},
        )
    if project_fp is not None and not _FP_RE.fullmatch(project_fp):
        raise InvalidInputError("invalid project fingerprint", details={"field": "project_fp"})
    target = Path(root).expanduser()
    if not target.is_absolute() or not target.is_dir():
        raise InvalidInputError(
            "root must be an absolute path to an existing directory", details={"field": "root"}
        )
    target = target.resolve()
    fingerprint = _registered_fp(target, config_loader(target), project_fp)
    source_id = source_id_for(workspace_id)
    store.register_source(
        workspace_id, source_id, kind=SOURCE_KIND, display_name=SOURCE_DISPLAY_NAME
    )
    config = store.source_config(source_id)
    links = {pid: link for pid, link in parse_links(config).items() if pid != project_id}
    clash = sorted(
        pid
        for pid, link in links.items()
        if link.project_fp == fingerprint and link.root == str(target)
    )
    if clash:
        raise InvalidInputError(
            "that project-context project is already linked to another project",
            details={"project_ids": clash},
        )
    links[project_id] = ProjectLink(project_id, str(target), fingerprint)
    store.set_source_config(source_id, _config_of(config, links))
    _logger.info("linked project %s to project-context %s", project_id, fingerprint)
    return {
        "source_id": source_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "project_fp": fingerprint,
        "linked": True,
    }


def unlink_project(store: KnowledgeStore, workspace_id: str, project_id: str) -> dict[str, Any]:
    """Remove the link of ``project_id`` (its documents stop being searchable)."""
    store.get_project(project_id)
    source_id = source_id_for(workspace_id)
    config = store.source_config(source_id)
    links = parse_links(config)
    removed = links.pop(project_id, None)
    if removed is not None:
        store.set_source_config(source_id, _config_of(config, links))
    return {
        "source_id": source_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "project_fp": removed.project_fp if removed is not None else None,
        "linked": False,
    }


def _config_of(config: dict[str, Any], links: dict[str, ProjectLink]) -> dict[str, Any]:
    return {
        **config,
        "links": {
            pid: {"root": link.root, "project_fp": link.project_fp}
            for pid, link in sorted(links.items())
        },
    }


__all__ = [
    "SOURCE_KIND",
    "ProjectContextAdapter",
    "ProjectLink",
    "link_project",
    "parse_links",
    "source_id_for",
    "unlink_project",
]
