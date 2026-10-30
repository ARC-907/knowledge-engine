"""The source-adapter protocol and its capability descriptor (contract version 1).

A source adapter owns one source's search/open behaviour (the corpus FTS and
embedding indexes, project context, the board, an external Serena MCP
server ...). The broker (a later slice) resolves scope, passes the
:class:`~knowledge_engine.knowledge.contracts.EffectiveScope` filters to each
adapter *before* candidate ranking and limits, enforces deadlines and byte
caps, and normalizes the results. Adapters never widen scope: a record outside
the effective scope must not be returned, and ``open`` must recheck scope.

The design labels the calls ``search(context, query)`` and
``open(context, record_id, revision_id, locator)``. Both also receive the
resolved :class:`~knowledge_engine.knowledge.contracts.EffectiveScope`
(ADR 0006): in a :class:`QueryContext` an omitted selection means "use the
profile", and adapters must never resolve profiles or ids themselves, so the
broker hands them the concrete scope alongside the caller's budgets.

Adapters raise :class:`~knowledge_engine.knowledge.errors.KnowledgeError`
subclasses; anything else is treated as ``internal_error`` by the broker.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from .contracts import (
    CONTRACT_VERSION,
    ContractModel,
    EffectiveScope,
    Locator,
    OpenResult,
    QueryContext,
    RecordId,
    RevisionId,
    SchemaVersion,
    SearchHit,
    SearchMode,
    SourceId,
    SourceStatus,
)

#: Filter dimensions an adapter can apply natively (before its own ranking/limit).
FilterDimension = Literal["project", "collection", "research_type", "tag"]
LocatorKind = Literal["text", "pdf", "transcript", "code", "unsupported"]


class AdapterCapabilities(ContractModel):
    """What one adapter can do; discovery data, never a permission grant."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    source_kind: str = Field(min_length=1, max_length=64, description="e.g. corpus, serena")
    contract_version: Literal[1] = CONTRACT_VERSION
    modes: tuple[SearchMode, ...] = Field(min_length=1)
    filters: tuple[FilterDimension, ...] = ()
    locator_kinds: tuple[LocatorKind, ...] = ()
    supports_open: bool = False
    supports_cursor: bool = False
    read_only: bool = True
    max_limit: int = Field(default=100, ge=1, le=1000)


class AdapterSearchResult(ContractModel):
    """Hits of one adapter for one request, with its status."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    hits: tuple[SearchHit, ...] = ()
    status: SourceStatus
    next_cursor: str | None = Field(default=None, max_length=2000)


@runtime_checkable
class SourceAdapter(Protocol):
    """The interface every source adapter implements."""

    @property
    def source_id(self) -> str:
        """The namespaced id of the source this adapter serves."""
        ...

    def describe_capabilities(self) -> AdapterCapabilities:
        """Static capabilities (no I/O beyond reading configuration)."""
        ...

    def health(self) -> SourceStatus:
        """A bounded health probe, separate from discovery."""
        ...

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        """Search within ``scope`` (already validated); filters apply before limits."""
        ...

    def open(
        self,
        context: QueryContext,
        scope: EffectiveScope,
        record_id: RecordId,
        revision_id: RevisionId,
        locator: Locator | None = None,
    ) -> OpenResult:
        """Return the exact revision (never silently the latest); recheck scope first."""
        ...


__all__ = [
    "AdapterCapabilities",
    "AdapterSearchResult",
    "FilterDimension",
    "LocatorKind",
    "SourceAdapter",
]
