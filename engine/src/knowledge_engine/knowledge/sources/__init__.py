"""Built-in source adapters of the retrieval broker and the default adapter factory.

* :mod:`.corpus` -- ``corpus:base``: the FTS and embedding indexes of the corpus
  registry (:class:`~.corpus.CorpusAdapter`);
* :mod:`.project_context` -- ``project_context:<workspace>``: linked
  project-context stores (:class:`~.project_context.ProjectContextAdapter`);
* the agent board is **not** a broker source: board posts are coordination
  messages, not research records, and searching them must be an explicit
  opt-in. :data:`BOARD_SOURCE_AVAILABLE` is ``False`` and a ``board`` source
  is reported ``unavailable`` with that reason; a future opt-in adapter plugs
  in here.

:func:`build_adapter_factory` maps a source of the request snapshot to its
adapter; the broker calls it once per selected source and request.
"""

from __future__ import annotations

from ..adapter import SourceAdapter
from ..backfill import CORPUS_SOURCE_ID
from ..broker import AdapterFactory
from ..errors import UnavailableError
from ..store import ScopeSnapshot, SnapshotSource
from .corpus import CorpusAdapter, CorpusBackend
from .project_context import SOURCE_KIND as PROJECT_CONTEXT_KIND, ProjectContextAdapter

#: The board is not a broker source by default (explicit opt-in; no adapter yet).
BOARD_SOURCE_AVAILABLE = False


def build_adapter_factory(
    corpus: CorpusBackend | None, *, project_context: bool = True
) -> AdapterFactory:
    """The default factory: corpus and project-context adapters; others are unavailable."""

    def factory(source: SnapshotSource, snapshot: ScopeSnapshot) -> SourceAdapter:
        if source.kind == "corpus":
            if corpus is None or source.source_id != CORPUS_SOURCE_ID:
                raise UnavailableError(
                    f"no corpus adapter is configured for source {source.source_id!r}",
                    details={"source_id": source.source_id},
                )
            return CorpusAdapter(source.source_id, corpus, snapshot)
        if source.kind == PROJECT_CONTEXT_KIND and project_context:
            return ProjectContextAdapter(source.source_id, snapshot)
        if source.kind == "board" and not BOARD_SOURCE_AVAILABLE:
            raise UnavailableError(
                "the agent board is not a search source (explicit opt-in; not available)",
                details={"source_id": source.source_id},
            )
        raise UnavailableError(
            f"no adapter is available for source {source.source_id!r} of kind {source.kind!r}",
            details={"source_id": source.source_id, "kind": source.kind},
        )

    return factory


__all__ = [
    "BOARD_SOURCE_AVAILABLE",
    "CorpusAdapter",
    "CorpusBackend",
    "ProjectContextAdapter",
    "build_adapter_factory",
]
