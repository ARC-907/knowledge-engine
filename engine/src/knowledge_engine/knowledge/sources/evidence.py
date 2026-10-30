"""Broker adapter for sources whose records live only in the evidence store.

Module output (``module:<id>`` sources, see
:mod:`knowledge_engine.modules.evidence_ingestion`) has no index of its own:
its records and revisions are rows of ``knowledge.db``. This adapter makes
them searchable and openable like any other source:

* **Search** (keyword) scans the in-scope records of the request snapshot
  (scope and filters already applied by the store), reads each record's
  current revision from the evidence store and ranks matches with BM25 over
  the decoded text (HTML tags are stripped for matching and excerpts). The
  scan is bounded: at most :data:`MAX_SCAN_RECORDS` records and
  :data:`MAX_SCAN_BYTES` bytes per revision; a cut scan is reported as a
  warning, never silently.
* ``vector`` mode is ``unavailable`` (no embeddings are kept for these
  records); ``hybrid`` falls back to keyword ranking with a warning, so the
  other sources still fuse.
* **Open** serves the exact requested revision through
  :func:`~knowledge_engine.knowledge.evidence.resolve.open_evidence`
  (verified, stale, unavailable or unsupported; never the latest instead).
"""

from __future__ import annotations

import html
import logging
import math
import re
import time
from collections import Counter

from ..adapter import AdapterCapabilities, AdapterSearchResult
from ..contracts import (
    EffectiveScope,
    ErrorInfo,
    Locator,
    OpenResult,
    QueryContext,
    RankingInfo,
    RecordScope,
    SearchHit,
    SearchMode,
    SourceState,
    SourceStatus,
)
from ..errors import UnavailableError, UnknownScopeError
from ..evidence.store import EvidenceStore
from ..store import ScopeSnapshot, SnapshotRecord
from ._common import MAX_EXCERPT_CHARS, clip

_logger = logging.getLogger(__name__)

#: Source kinds served by this adapter.
EVIDENCE_SOURCE_KINDS = frozenset({"module"})
#: Most in-scope records one search reads.
MAX_SCAN_RECORDS = 2000
#: Most bytes of one revision a search decodes.
MAX_SCAN_BYTES = 1024 * 1024
_EXCERPT_WINDOW = 400
_K1 = 1.2
_B = 0.75
_TOKEN = re.compile(r"\w+", re.UNICODE)
_TAG = re.compile(r"<[^>]{0,2000}>")
_SPACE = re.compile(r"\s+")
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


def _plain(media_type: str, data: bytes) -> str | None:
    """Decoded, whitespace-normalized text of a revision, or ``None`` if not text."""
    if not media_type.startswith(_TEXT_TYPES):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "html" in media_type or "xml" in media_type:
        text = html.unescape(_TAG.sub(" ", text))
    return _SPACE.sub(" ", text).strip()


def _excerpt(text: str, terms: set[str]) -> str:
    lowered = text.lower()
    first = min((i for t in terms if (i := lowered.find(t)) >= 0), default=0)
    start = max(0, first - _EXCERPT_WINDOW // 4)
    snippet = text[start : start + _EXCERPT_WINDOW]
    return ("..." if start else "") + snippet


class EvidenceSourceAdapter:
    """Search/open of an evidence-only source within one snapshot's scope."""

    def __init__(
        self,
        source_id: str,
        source_kind: str,
        evidence: EvidenceStore,
        snapshot: ScopeSnapshot,
        *,
        profile: str = "default",
    ) -> None:
        self._source_id = source_id
        self._kind = source_kind
        self.evidence = evidence
        self.profile = profile
        self._records: dict[str, SnapshotRecord] = snapshot.records.get(source_id, {})
        self.warnings: list[str] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    def describe_capabilities(self) -> AdapterCapabilities:
        modes: tuple[SearchMode, ...] = ("fts", "hybrid")
        return AdapterCapabilities(
            source_id=self._source_id,
            source_kind=self._kind,
            modes=modes,
            filters=("project", "collection", "research_type", "tag"),
            locator_kinds=("text", "unsupported"),
            supports_open=True,
        )

    def health(self) -> SourceStatus:
        if self.evidence.store.closed:
            return SourceStatus(
                source_id=self._source_id,
                state=SourceState.UNAVAILABLE,
                error=ErrorInfo(code="unavailable", message="the knowledge store is closed"),
            )
        return SourceStatus(source_id=self._source_id, state=SourceState.OK)

    # ── search ───────────────────────────────────────────────────

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        started = time.monotonic()
        if context.mode == "vector":
            raise UnavailableError(
                f"vector search is unavailable for {self._source_id}: module evidence has no "
                "embeddings; use fts or hybrid",
                details={"source_id": self._source_id, "mode": "vector"},
            )
        if context.mode == "hybrid":
            self.warnings.append("keyword ranking only (module evidence has no embeddings)")
        terms = [t.lower() for t in _TOKEN.findall(query)]
        hits: list[SearchHit] = []
        if terms and not scope.empty and self._source_id in scope.source_ids and self._records:
            ranked = self._rank(terms, context.limit)
            hits = [
                self._hit(rank, record, revision_id, score, text, set(terms), scope)
                for rank, (record, revision_id, score, text) in enumerate(ranked, start=1)
            ]
        return AdapterSearchResult(
            source_id=self._source_id,
            hits=tuple(hits),
            status=SourceStatus(
                source_id=self._source_id,
                state=SourceState.OK,
                hit_count=len(hits),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
        )

    def _documents(self) -> list[tuple[SnapshotRecord, str, str]]:
        """``(record, current revision id, text)`` of the in-scope text records (bounded)."""
        records = sorted(self._records.values(), key=lambda r: r.view.record_id)
        if len(records) > MAX_SCAN_RECORDS:
            self.warnings.append(
                f"only the first {MAX_SCAN_RECORDS} of {len(records)} in-scope records were "
                "searched"
            )
            records = records[:MAX_SCAN_RECORDS]
        docs: list[tuple[SnapshotRecord, str, str]] = []
        truncated = 0
        with self.evidence.snapshot():
            for record in records:
                state = self.evidence.record_state(self._source_id, record.view.record_id)
                if state.deleted_at is not None or state.current_revision_id is None:
                    continue
                info = self.evidence.get_revision(state.current_revision_id)
                if info is None or not info.revision.bytes_retained:
                    continue
                data = self.evidence.revision_bytes(state.current_revision_id)
                if data is None:
                    continue
                if len(data) > MAX_SCAN_BYTES:
                    truncated += 1
                    data = data[:MAX_SCAN_BYTES]
                text = _plain(info.revision.media_type, data)
                if text:
                    docs.append((record, state.current_revision_id, text))
        if truncated:
            self.warnings.append(
                f"{truncated} revision(s) larger than {MAX_SCAN_BYTES} bytes were searched "
                "partially"
            )
        return docs

    def _rank(self, terms: list[str], limit: int) -> list[tuple[SnapshotRecord, str, float, str]]:
        docs = self._documents()
        if not docs:
            return []
        tokenized = [Counter(t.lower() for t in _TOKEN.findall(text)) for _, _, text in docs]
        lengths = [sum(c.values()) for c in tokenized]
        average = (sum(lengths) / len(lengths)) or 1.0
        unique = set(terms)
        frequency = {t: sum(1 for c in tokenized if c[t]) for t in unique}
        scored: list[tuple[float, int]] = []
        for index, counts in enumerate(tokenized):
            score = 0.0
            for term in unique:
                tf = counts[term]
                if not tf:
                    continue
                df = frequency[term]
                idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
                norm = tf + _K1 * (1 - _B + _B * lengths[index] / average)
                score += idf * tf * (_K1 + 1) / norm
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], docs[item[1]][0].view.record_id))
        return [
            (docs[i][0], docs[i][1], round(score, 6), docs[i][2]) for score, i in scored[:limit]
        ]

    def _hit(
        self,
        rank: int,
        record: SnapshotRecord,
        revision_id: str,
        score: float,
        text: str,
        terms: set[str],
        scope: EffectiveScope,
    ) -> SearchHit:
        provenance = self.evidence.derivations_producing(revision_id)
        return SearchHit(
            source_id=self._source_id,
            record_id=record.view.record_id,
            revision_id=revision_id,
            scope=RecordScope(workspace_id=scope.workspace_id, project_id=record.view.project_id),
            collection_ids=tuple(record.view.collection_ids),
            research_type=record.view.research_type,
            title=clip(record.title or record.logical_path or record.view.record_id, 1000),
            excerpt=clip(_excerpt(text, terms), MAX_EXCERPT_CHARS),
            provenance_id=clip(f"run:{provenance[-1].run_id}", 200) if provenance else None,
            ranking=RankingInfo(native_rank=rank, native_score=score, score_kind="bm25"),
        )

    # ── open ─────────────────────────────────────────────────────

    def open(
        self,
        context: QueryContext,
        scope: EffectiveScope,
        record_id: str,
        revision_id: str,
        locator: Locator | None = None,
    ) -> OpenResult:
        if record_id not in self._records:
            raise UnknownScopeError(
                f"unknown record {self._source_id}/{record_id} in the effective scope",
                details={"kind": "record", "source_id": self._source_id, "id": record_id},
            )
        from ..evidence.resolve import open_evidence  # noqa: PLC0415 - avoid an import cycle

        resolution = open_evidence(
            self.evidence,
            context,
            source_id=self._source_id,
            record_id=record_id,
            revision_id=revision_id,
            locator=locator,
            configured_profile=self.profile,
        )
        _logger.debug(
            "opened %s/%s@%s from the evidence store (%s)",
            self._source_id,
            record_id,
            revision_id,
            resolution.verification.value,
        )
        return resolution.result


__all__ = [
    "EVIDENCE_SOURCE_KINDS",
    "MAX_SCAN_BYTES",
    "MAX_SCAN_RECORDS",
    "EvidenceSourceAdapter",
]
