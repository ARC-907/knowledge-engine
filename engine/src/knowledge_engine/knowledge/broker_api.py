"""Request models and entry points shared by the HTTP, MCP and CLI bindings of the broker.

The three surfaces translate transport details only: each validates its input
through :class:`SearchRequest` / :class:`OpenRequest` and calls
:func:`run_search` / :func:`run_open` on one
:class:`~knowledge_engine.knowledge.broker.KnowledgeBroker`, so they return
the same JSON (a :class:`~.contracts.SearchEnvelope` or an
:class:`~.contracts.OpenResult`) and the same typed errors.

:class:`BrokerEngine` opens a self-contained broker (registry, FTS index,
embedding index, ``knowledge.db``) for the CLI and the MCP tool group; the
HTTP app builds its broker around the objects of its lifespan instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .broker import MAX_QUERY_CHARS, KnowledgeBroker
from .contracts import (
    Locator,
    RecordId,
    RevisionId,
    SearchEnvelope,
    SearchHit,
    SourceId,
    parse_query_context,
    validation_messages,
)
from .errors import InvalidInputError

if TYPE_CHECKING:
    from ..core.registry import Registry
    from ..core.settings import Settings
    from ..retrieval.fts5 import Indexer
    from .evidence.policy import EvidencePolicy
    from .store import KnowledgeStore

_logger = logging.getLogger(__name__)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchRequest(_Request):
    """One broker search: the query text and its query context."""

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="FTS5 query text (vector/hybrid: natural language).",
    )
    context: dict[str, Any] = Field(
        description="A QueryContext: workspace_id is required; project_id, profile_id, "
        "source_ids, collection_ids, research_types, tags, include_workspace, mode, limit, "
        "timeout_ms and byte_budget are optional (see the knowledge contracts)."
    )


class OpenRequest(_Request):
    """Open one record of the scope at a revision (the current one when omitted)."""

    context: dict[str, Any] = Field(description="A QueryContext; the record must be in its scope.")
    source_id: SourceId
    record_id: RecordId
    revision_id: RevisionId | None = Field(
        default=None, description="The exact revision (from a search hit); omit for the current."
    )
    locator: Locator | None = Field(default=None, description="Optional text locator to echo.")


class KnowledgeSearchHit(SearchHit):
    """:class:`~.contracts.SearchHit` under a name distinct from the legacy ``/search`` hit.

    Only the OpenAPI component name differs (the legacy ``SearchHit`` schema of
    ``GET /search`` keeps its name); the fields are the contract's.
    """


class KnowledgeSearchEnvelope(SearchEnvelope):
    """:class:`~.contracts.SearchEnvelope` whose hits document as ``KnowledgeSearchHit``."""

    hits: tuple[KnowledgeSearchHit, ...] = ()


def _validate[M: BaseModel](model: type[M], payload: Any, what: str) -> M:
    if not isinstance(payload, dict):
        raise InvalidInputError(f"{what} arguments must be a JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise InvalidInputError(
            f"invalid {what} arguments", details={"errors": validation_messages(exc)}
        ) from exc


def run_search(broker: KnowledgeBroker, payload: Any) -> dict[str, Any]:
    """Validate ``payload`` as a :class:`SearchRequest`, search, return the envelope JSON."""
    request = _validate(SearchRequest, payload, "search")
    context = parse_query_context(request.context)
    return broker.search(context, request.query).model_dump(mode="json")


def run_open(broker: KnowledgeBroker, payload: Any) -> dict[str, Any]:
    """Validate ``payload`` as an :class:`OpenRequest`, open, return the result JSON."""
    request = _validate(OpenRequest, payload, "open")
    context = parse_query_context(request.context)
    result = broker.open(
        context, request.source_id, request.record_id, request.revision_id, request.locator
    )
    return result.model_dump(mode="json")


def build_broker(
    store: KnowledgeStore,
    registry: Registry,
    indexer: Indexer,
    *,
    corpus_root: Any,
    embeddings: Any = None,
    embeddings_error: str | None = None,
    profile: str = "default",
    evidence_policy: EvidencePolicy | None = None,
) -> KnowledgeBroker:
    """A broker over existing engine objects (owned by the caller, except the pool).

    The corpus adapter reads recorded revisions from the evidence store in the
    same ``knowledge.db`` (``evidence_policy``: the operator's retention policy).
    """
    from .evidence.store import EvidenceStore  # noqa: PLC0415
    from .sources import CorpusBackend, build_adapter_factory  # noqa: PLC0415 - heavy imports

    backend = CorpusBackend(
        indexer=indexer,
        registry=registry,
        corpus_root=corpus_root,
        embeddings=embeddings,
        embeddings_error=embeddings_error,
        evidence=EvidenceStore(store, evidence_policy),
        profile=profile,
    )
    return KnowledgeBroker(store, build_adapter_factory(backend, profile=profile), profile=profile)


@dataclass
class BrokerEngine:
    """A self-contained broker and the resources it opened; :meth:`close` releases them."""

    broker: KnowledgeBroker
    store: KnowledgeStore
    registry: Registry
    indexer: Indexer

    @classmethod
    def open(cls, settings: Settings | None = None) -> BrokerEngine:
        """Open the registry, the FTS index, the embedding index and ``knowledge.db``."""
        from ..core.config import Config  # noqa: PLC0415
        from ..core.registry import Registry  # noqa: PLC0415
        from ..core.settings import Settings  # noqa: PLC0415
        from ..retrieval.embeddings.index import open_index  # noqa: PLC0415
        from ..retrieval.fts5 import Indexer  # noqa: PLC0415
        from .config import KnowledgeConfig  # noqa: PLC0415
        from .store import KnowledgeStore  # noqa: PLC0415

        resolved = settings if settings is not None else Settings()
        knowledge = KnowledgeConfig.from_settings(resolved)
        config = Config.from_settings(resolved)
        config.ensure_dirs()
        registry = Registry(config.registry_path)
        opened: list[Any] = [registry]
        try:
            indexer = Indexer(config, registry)
            opened.append(indexer)
            store = KnowledgeStore.open(knowledge.db_path)
            opened.append(store)
            embeddings, error = open_index(resolved)
            broker = build_broker(
                store,
                registry,
                indexer,
                corpus_root=config.corpus_root,
                embeddings=embeddings,
                embeddings_error=error,
                profile=knowledge.profile,
                evidence_policy=knowledge.evidence,
            )
        except Exception:
            for resource in reversed(opened):
                resource.close()
            raise
        return cls(broker=broker, store=store, registry=registry, indexer=indexer)

    def close(self) -> None:
        """Close the broker pool and every handle (each failure is logged)."""
        for name, resource in (
            ("broker", self.broker),
            ("knowledge store", self.store),
            ("index", self.indexer),
            ("registry", self.registry),
        ):
            try:
                resource.close()
            except Exception:
                _logger.exception("closing the %s failed", name)


__all__ = [
    "BrokerEngine",
    "KnowledgeSearchEnvelope",
    "KnowledgeSearchHit",
    "OpenRequest",
    "SearchRequest",
    "build_broker",
    "run_open",
    "run_search",
]
