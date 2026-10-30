"""The retrieval broker: one scoped search/open service over every source adapter (V2 P2).

:class:`KnowledgeBroker` implements the sequence of the V2 design ("Query and
adapter contracts") for every binding (HTTP ``/knowledge/search`` and
``/knowledge/open``, the ``knowledge.search`` / ``knowledge.open`` MCP tools and
``knowledge-engine knowledge search|open``):

1. validate the query and the :class:`~.contracts.QueryContext`; resolve the
   allowed configured sources of the workspace/project/profile;
2. take one consistent metadata snapshot (:meth:`.store.KnowledgeStore.snapshot`):
   the effective scope, the workspace's sources and every in-scope record;
3. bind one adapter per selected source to that snapshot and hand it the
   scope, so filters apply *before* the adapter ranks and limits;
4. run the adapters concurrently on a bounded thread pool under the request
   deadline (``timeout_ms``); a source that misses it is reported ``timeout``
   and its late result is ignored; enforce the per-source candidate cap
   (``limit``) and the byte budget;
5. normalize hits, keeping each source's native rank/score as provenance --
   raw BM25 and cosine values are never compared;
6. recheck visibility against the snapshot, deduplicate by source-owned
   ``(source_id, record_id, revision_id)`` identity (merging collection
   membership), and fuse the per-source rankings with reciprocal rank fusion
   whose ties break on that identity, so the order is deterministic;
7. apply the final limit and return a :class:`~.contracts.SearchEnvelope` with
   the effective scope, one :class:`~.contracts.SourceStatus` per selected
   source, ``partial`` and warnings.

Outcomes: an explicit empty selection is an empty success with no sources; no
matches is an empty success with healthy statuses; an unavailable source with
others answering gives ``partial=true``; when every selected source fails --
including a mode (``vector``/``hybrid``) no selected source can serve -- the
request fails with ``unavailable`` instead of returning an empty list. Scope
errors (unknown ids, invalid scope, denied principal) fail the request.

Each source contributes at most ``limit`` candidates. Because fusion keeps
every source's own order, the fused top ``limit`` of those candidates equals
the fused top ``limit`` of the sources' complete rankings; the approximation
is inside a source (``hybrid`` fuses bounded FTS and vector candidates). See
``02-dev-docs/adr/0011-retrieval-broker-fusion-and-adapters.md``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .adapter import AdapterCapabilities, AdapterSearchResult, SourceAdapter
from .contracts import (
    FAILED_SOURCE_STATES,
    EffectiveScope,
    ErrorInfo,
    Locator,
    OpenResult,
    QueryContext,
    ResearchType,
    SearchEnvelope,
    SearchHit,
    SourceState,
    SourceStatus,
)
from .errors import (
    DeniedError,
    InternalError,
    InvalidInputError,
    InvalidScopeError,
    KnowledgeError,
    KnowledgeTimeoutError,
    OutputInvalidError,
    UnavailableError,
    UnknownScopeError,
)
from .scope import DEFAULT_PROFILE_ID, RecordView, record_in_scope
from .store import KnowledgeStore, ScopeSnapshot, SnapshotSource

_logger = logging.getLogger(__name__)

#: Adapters running at once per broker (the pool is shared by every request).
MAX_CONCURRENCY = 4
#: The reciprocal-rank-fusion constant (the legacy hybrid search uses the same value).
RRF_K = 60
#: Longest accepted query, in characters.
MAX_QUERY_CHARS = 1000
#: Most warnings one envelope carries.
MAX_WARNINGS = 50

#: Builds the adapter of one source for one request; raises a ``KnowledgeError``
#: (normally ``unavailable``) when the source has no adapter.
AdapterFactory = Callable[[SnapshotSource, ScopeSnapshot], SourceAdapter]


@runtime_checkable
class RevisionResolver(Protocol):
    """Optional adapter extension: the current revision of a record (open without a revision)."""

    def current_revision(self, context: QueryContext, scope: EffectiveScope, record_id: str) -> str:
        """The revision id of the record's current content; typed errors as ``open``."""


def validate_query(query: object) -> str:
    """The stripped query text, or ``invalid_input``."""
    if not isinstance(query, str) or not query.strip():
        raise InvalidInputError("the query must be a non-empty string", details={"field": "query"})
    text = query.strip()
    if len(text) > MAX_QUERY_CHARS:
        raise InvalidInputError(
            f"the query is longer than {MAX_QUERY_CHARS} characters",
            details={"field": "query", "max_chars": MAX_QUERY_CHARS},
        )
    return text


def _hit_bytes(hit: SearchHit) -> int:
    return len(hit.title.encode("utf-8")) + len(hit.excerpt.encode("utf-8"))


def _state_of(exc: KnowledgeError) -> SourceState:
    if isinstance(exc, KnowledgeTimeoutError):
        return SourceState.TIMEOUT
    if isinstance(exc, UnavailableError):
        return SourceState.UNAVAILABLE
    return SourceState.ERROR


def _failed(source_id: str, exc: KnowledgeError, elapsed_ms: int | None = None) -> SourceStatus:
    return SourceStatus(
        source_id=source_id,
        state=_state_of(exc),
        error=ErrorInfo.model_validate(exc.to_dict()),
        elapsed_ms=elapsed_ms,
    )


#: Errors an adapter raises that fail the whole request instead of one source.
_REQUEST_ERRORS = (InvalidScopeError, UnknownScopeError, DeniedError)


@dataclass
class _Outcome:
    """What one source produced for one request."""

    status: SourceStatus
    hits: list[SearchHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class KnowledgeBroker:
    """Scoped search and open over the sources of ``store``'s workspaces.

    ``factory`` binds an adapter to a source for one request. The broker owns a
    bounded thread pool; :meth:`close` releases it (late adapter calls are not
    waited for).
    """

    def __init__(
        self,
        store: KnowledgeStore,
        factory: AdapterFactory,
        *,
        profile: str = DEFAULT_PROFILE_ID,
        max_concurrency: int = MAX_CONCURRENCY,
        rrf_k: int = RRF_K,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if rrf_k < 0:
            raise ValueError("rrf_k must be >= 0")
        self.store = store
        self.factory = factory
        self.profile = profile
        self.rrf_k = rrf_k
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency, thread_name_prefix="ke-broker"
        )
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Stop accepting work; queued adapter calls are cancelled, running ones abandoned."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        _logger.debug("retrieval broker closed")

    # ── search ───────────────────────────────────────────────────

    def search(self, context: QueryContext, query: str) -> SearchEnvelope:
        """Run one scoped search (see the module docstring for the semantics)."""
        text = validate_query(query)
        snapshot = self.store.snapshot(context, configured_profile=self.profile)
        scope = snapshot.scope
        if scope.empty or not scope.source_ids:
            return SearchEnvelope(effective_scope=scope)

        outcomes: dict[str, _Outcome] = {}
        runnable: dict[str, tuple[SourceAdapter, QueryContext]] = {}
        for source_id in scope.source_ids:
            bound = self._bind(source_id, snapshot)
            if isinstance(bound, KnowledgeError):
                outcomes[source_id] = _Outcome(_failed(source_id, bound))
                continue
            adapter, capabilities = bound
            if context.mode not in capabilities.modes:
                reason = UnavailableError(
                    f"source {source_id!r} does not support {context.mode!r} search",
                    details={"source_id": source_id, "modes": list(capabilities.modes)},
                )
                outcomes[source_id] = _Outcome(_failed(source_id, reason))
                continue
            cap = min(context.limit, capabilities.max_limit)
            runnable[source_id] = (adapter, context.model_copy(update={"limit": cap}))

        if not runnable:
            raise self._all_failed(outcomes, context, unsupported_mode=context.mode != "fts")
        outcomes.update(self._run(runnable, scope, text, context.timeout_ms))
        for source_id in scope.source_ids:
            outcome = outcomes[source_id]
            if outcome.status.state not in FAILED_SOURCE_STATES:
                outcome.hits = self._normalize(source_id, outcome, snapshot, context)
                outcome.status = outcome.status.model_copy(update={"hit_count": len(outcome.hits)})
        statuses = tuple(outcomes[source_id].status for source_id in scope.source_ids)
        if all(status.state in FAILED_SOURCE_STATES for status in statuses):
            raise self._all_failed(outcomes, context)

        warnings: list[str] = []
        for source_id in scope.source_ids:
            warnings.extend(f"{source_id}: {w}" for w in outcomes[source_id].warnings)
        hits = self._fuse([outcomes[sid].hits for sid in scope.source_ids])
        hits, cut = _within_budget(hits[: context.limit], context.byte_budget)
        if cut:
            warnings.append(f"byte budget of {context.byte_budget} bytes reached; {cut} hit(s) cut")
        return SearchEnvelope(
            hits=tuple(hits),
            effective_scope=scope,
            source_status=statuses,
            partial=any(status.state != SourceState.OK for status in statuses),
            warnings=tuple(warnings[:MAX_WARNINGS]),
        )

    def _bind(
        self, source_id: str, snapshot: ScopeSnapshot
    ) -> tuple[SourceAdapter, AdapterCapabilities] | KnowledgeError:
        source = snapshot.sources.get(source_id)
        if source is None:  # resolve_scope only selects known sources
            return InternalError(f"source {source_id!r} missing from the snapshot")
        try:
            adapter = self.factory(source, snapshot)
            capabilities = adapter.describe_capabilities()
        except KnowledgeError as exc:
            return exc
        except Exception:
            _logger.exception("binding the adapter of source %s failed", source_id)
            return InternalError(f"the adapter of source {source_id!r} failed to start")
        if capabilities.source_id != source_id:
            return OutputInvalidError(f"the adapter of {source_id!r} describes another source")
        return adapter, capabilities

    def _all_failed(
        self,
        outcomes: dict[str, _Outcome],
        context: QueryContext,
        *,
        unsupported_mode: bool = False,
    ) -> UnavailableError:
        statuses = [outcomes[sid].status.model_dump(mode="json") for sid in sorted(outcomes)]
        _logger.warning("knowledge search (%s): every selected source failed", context.mode)
        message = (
            f"{context.mode} search is unavailable for every selected source"
            if unsupported_mode
            else "every selected source failed; no results can be returned"
        )
        return UnavailableError(message, details={"mode": context.mode, "source_status": statuses})

    def _run(
        self,
        runnable: dict[str, tuple[SourceAdapter, QueryContext]],
        scope: EffectiveScope,
        query: str,
        timeout_ms: int,
    ) -> dict[str, _Outcome]:
        deadline = time.monotonic() + timeout_ms / 1000
        futures: dict[str, Future[tuple[AdapterSearchResult, int, list[str]]]] = {}
        outcomes: dict[str, _Outcome] = {}
        for source_id, (adapter, ctx) in sorted(runnable.items()):
            try:
                futures[source_id] = self._executor.submit(_call, adapter, ctx, scope, query)
            except RuntimeError:
                outcomes[source_id] = _Outcome(
                    _failed(source_id, UnavailableError("the retrieval broker is closed"))
                )
        wait(futures.values(), timeout=max(0.0, deadline - time.monotonic()))
        for source_id, future in futures.items():
            if not future.done():
                future.cancel()
                future.add_done_callback(_log_late(source_id))
                _logger.warning("source %s missed the %d ms deadline", source_id, timeout_ms)
                outcomes[source_id] = _Outcome(
                    _failed(
                        source_id,
                        KnowledgeTimeoutError(
                            f"source {source_id!r} did not answer within {timeout_ms} ms",
                            details={"timeout_ms": timeout_ms},
                        ),
                        elapsed_ms=timeout_ms,
                    )
                )
                continue
            exc = future.exception()
            if isinstance(exc, _REQUEST_ERRORS):
                raise exc
            if isinstance(exc, KnowledgeError):
                _logger.info("source %s failed: %s (%s)", source_id, exc.message, exc.code)
                outcomes[source_id] = _Outcome(_failed(source_id, exc))
                continue
            if exc is not None:
                _logger.error("source %s raised an unexpected error", source_id, exc_info=exc)
                outcomes[source_id] = _Outcome(
                    _failed(source_id, InternalError(f"source {source_id!r} failed unexpectedly"))
                )
                continue
            result, elapsed_ms, warnings = future.result()
            outcomes[source_id] = _accept(source_id, result, elapsed_ms, warnings)
        return outcomes

    def _normalize(
        self,
        source_id: str,
        outcome: _Outcome,
        snapshot: ScopeSnapshot,
        context: QueryContext,
    ) -> list[SearchHit]:
        """Recheck visibility, align metadata with the snapshot, dedupe, cap and budget."""
        kept: dict[tuple[str, str, str], SearchHit] = {}
        hidden = merged = 0
        for hit in outcome.hits:
            aligned = _visible(hit, snapshot)
            if aligned is None:
                hidden += 1
                continue
            previous = kept.get(aligned.identity)
            if previous is None:
                kept[aligned.identity] = aligned
                continue
            merged += 1
            union = tuple(sorted(set(previous.collection_ids) | set(aligned.collection_ids)))
            kept[aligned.identity] = previous.model_copy(update={"collection_ids": union})
        if hidden:
            _logger.warning("source %s returned %d hit(s) outside the scope", source_id, hidden)
            outcome.warnings.append(f"{hidden} hit(s) outside the effective scope were dropped")
        if merged:
            outcome.warnings.append(f"{merged} repeated hit(s) of one record were merged")
        hits = list(kept.values())
        if len(hits) > context.limit:
            outcome.warnings.append(f"{len(hits) - context.limit} candidate(s) over the cap cut")
            hits = hits[: context.limit]
        hits, cut = _within_budget(hits, context.byte_budget)
        if cut:
            outcome.warnings.append(f"byte budget reached; {cut} candidate(s) cut")
        return hits

    def _fuse(self, rankings: Iterable[list[SearchHit]]) -> list[SearchHit]:
        """Reciprocal rank fusion of per-source rankings; ties break on hit identity."""
        scores: dict[tuple[str, str, str], float] = {}
        best: dict[tuple[str, str, str], SearchHit] = {}
        for ranking in rankings:
            for position, hit in enumerate(ranking, start=1):
                key = hit.identity
                scores[key] = scores.get(key, 0.0) + 1.0 / (self.rrf_k + position)
                current = best.get(key)
                if current is None:
                    best[key] = hit
                else:
                    union = tuple(sorted(set(current.collection_ids) | set(hit.collection_ids)))
                    best[key] = current.model_copy(update={"collection_ids": union})
        order = sorted(scores, key=lambda key: (-scores[key], key))
        fused: list[SearchHit] = []
        for rank, key in enumerate(order, start=1):
            hit = best[key]
            fused_ranking = hit.ranking.model_copy(
                update={"fused_rank": rank, "fused_score": scores[key]}
            )
            fused.append(hit.model_copy(update={"ranking": fused_ranking}))
        return fused

    # ── open ─────────────────────────────────────────────────────

    def open(
        self,
        context: QueryContext,
        source_id: str,
        record_id: str,
        revision_id: str | None = None,
        locator: Locator | None = None,
    ) -> OpenResult:
        """The exact revision of one in-scope record (the current one when omitted).

        A record outside the effective scope is reported exactly like an
        unknown one (``unknown_scope``), so open never reveals out-of-scope
        records. A revision the source cannot serve is reported by the
        adapter's ``verification`` state, never replaced by the latest.
        """
        snapshot = self.store.snapshot(context, configured_profile=self.profile)
        scope = snapshot.scope
        if source_id not in snapshot.sources:
            raise UnknownScopeError(
                f"unknown source {source_id!r} in workspace {scope.workspace_id!r}",
                details={"kind": "source", "id": source_id},
            )
        if source_id not in scope.source_ids:
            raise UnknownScopeError(
                f"source {source_id!r} is not in the effective scope",
                details={"kind": "source", "id": source_id},
            )
        if source_id in snapshot.record_sources and record_id not in snapshot.records.get(
            source_id, {}
        ):
            raise _unknown_record(source_id, record_id)
        bound = self._bind(source_id, snapshot)
        if isinstance(bound, KnowledgeError):
            raise bound
        adapter, capabilities = bound
        if not capabilities.supports_open:
            raise UnavailableError(f"source {source_id!r} does not support open")

        def run() -> OpenResult:
            revision = revision_id
            if revision is None:
                if not isinstance(adapter, RevisionResolver):
                    raise InvalidInputError(
                        f"source {source_id!r} needs an explicit revision_id",
                        details={"field": "revision_id"},
                    )
                revision = adapter.current_revision(context, scope, record_id)
            return adapter.open(context, scope, record_id, revision, locator)

        try:
            future = self._executor.submit(run)
        except RuntimeError as exc:
            raise UnavailableError("the retrieval broker is closed") from exc
        try:
            result = future.result(timeout=context.timeout_ms / 1000)
        except TimeoutError as exc:
            future.cancel()
            raise KnowledgeTimeoutError(
                f"source {source_id!r} did not open the record within {context.timeout_ms} ms",
                details={"timeout_ms": context.timeout_ms},
            ) from exc
        except KnowledgeError:
            raise
        except Exception as exc:
            _logger.exception("opening %s/%s failed", source_id, record_id)
            raise InternalError(f"source {source_id!r} failed to open the record") from exc
        return _check_open(result, snapshot, source_id, record_id, revision_id)


# ── helpers ──────────────────────────────────────────────────────────


def _call(
    adapter: SourceAdapter, context: QueryContext, scope: EffectiveScope, query: str
) -> tuple[AdapterSearchResult, int, list[str]]:
    started = time.monotonic()
    result = adapter.search(context, scope, query)
    elapsed = int((time.monotonic() - started) * 1000)
    raw = getattr(adapter, "warnings", ())
    warnings = [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []
    return result, elapsed, warnings


def _log_late(
    source_id: str,
) -> Callable[[Future[tuple[AdapterSearchResult, int, list[str]]]], None]:
    def callback(future: Future[tuple[AdapterSearchResult, int, list[str]]]) -> None:
        if not future.cancelled():
            _logger.debug("late result of source %s ignored", source_id)

    return callback


def _accept(source_id: str, result: object, elapsed_ms: int, warnings: list[str]) -> _Outcome:
    """Validate an adapter's answer; a malformed one becomes ``output_invalid``."""
    if not isinstance(result, AdapterSearchResult) or result.source_id != source_id:
        return _Outcome(
            _failed(
                source_id,
                OutputInvalidError(f"source {source_id!r} returned a malformed result"),
                elapsed_ms,
            )
        )
    stray = [hit for hit in result.hits if hit.source_id != source_id]
    status = result.status
    if status.source_id != source_id or stray:
        return _Outcome(
            _failed(
                source_id,
                OutputInvalidError(f"source {source_id!r} answered for another source"),
                elapsed_ms,
            )
        )
    if status.state in FAILED_SOURCE_STATES:
        return _Outcome(status.model_copy(update={"elapsed_ms": elapsed_ms}), [], warnings)
    status = status.model_copy(update={"elapsed_ms": elapsed_ms})
    return _Outcome(status, list(result.hits), list(warnings))


def _visible(hit: SearchHit, snapshot: ScopeSnapshot) -> SearchHit | None:
    """``hit`` aligned with the snapshot's metadata, or ``None`` when it is out of scope."""
    scope = snapshot.scope
    if hit.scope.workspace_id != scope.workspace_id or hit.source_id not in scope.source_ids:
        return None
    if hit.source_id in snapshot.record_sources:
        record = snapshot.records.get(hit.source_id, {}).get(hit.record_id)
        if record is None or record.view.project_id != hit.scope.project_id:
            return None
        return hit.model_copy(
            update={
                "collection_ids": tuple(sorted(record.view.collection_ids)),
                "research_type": record.view.research_type,
            }
        )
    view = RecordView(
        source_id=hit.source_id,
        record_id=hit.record_id,
        project_id=hit.scope.project_id,
        research_type=hit.research_type,
        collection_ids=frozenset(hit.collection_ids),
    )
    return hit if record_in_scope(view, scope) else None


def _within_budget(hits: list[SearchHit], budget: int) -> tuple[list[SearchHit], int]:
    total = 0
    for index, hit in enumerate(hits):
        total += _hit_bytes(hit)
        if total > budget:
            return hits[:index], len(hits) - index
    return hits, 0


def _unknown_record(source_id: str, record_id: str) -> UnknownScopeError:
    return UnknownScopeError(
        f"unknown record {source_id}/{record_id} in the effective scope",
        details={"kind": "record", "source_id": source_id, "id": record_id},
    )


def _check_open(
    result: object,
    snapshot: ScopeSnapshot,
    source_id: str,
    record_id: str,
    revision_id: str | None,
) -> OpenResult:
    if (
        not isinstance(result, OpenResult)
        or result.source_id != source_id
        or result.record_id != record_id
        or (revision_id is not None and result.revision_id != revision_id)
    ):
        raise OutputInvalidError(f"source {source_id!r} returned another record than requested")
    scope = snapshot.scope
    if result.scope.workspace_id != scope.workspace_id:
        raise _unknown_record(source_id, record_id)
    if source_id in snapshot.record_sources:
        record = snapshot.records.get(source_id, {}).get(record_id)
        if record is None or record.view.project_id != result.scope.project_id:
            raise _unknown_record(source_id, record_id)
        return result
    view = RecordView(
        source_id=source_id,
        record_id=record_id,
        project_id=result.scope.project_id,
        research_type=ResearchType.OTHER,
    )
    if not record_in_scope(view, scope):
        raise _unknown_record(source_id, record_id)
    return result


__all__ = [
    "MAX_CONCURRENCY",
    "MAX_QUERY_CHARS",
    "RRF_K",
    "AdapterFactory",
    "KnowledgeBroker",
    "RevisionResolver",
    "validate_query",
]
