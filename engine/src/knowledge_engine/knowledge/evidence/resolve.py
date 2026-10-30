"""Evidence resolution: ``open(context, record_id, revision_id, locator)`` (V2 design step 7).

:func:`open_evidence` is the evidence service function the retrieval broker
(P2) calls for its open path; it returns the P0
:class:`~knowledge_engine.knowledge.contracts.OpenResult` inside a
:class:`Resolution` that also carries the reason and the evidence identity.

Every call:

1. resolves the caller's :class:`~knowledge_engine.knowledge.contracts.QueryContext`
   and rechecks that the record is inside that scope (``denied`` otherwise);
2. loads the **exact** cited revision -- never the latest one;
3. verifies the retained bytes against the revision digest and slices the
   locator out of them;
4. answers one :class:`~knowledge_engine.knowledge.contracts.VerificationState`:

   * ``verified`` -- the cited revision is the record's current revision and
     the span is addressable;
   * ``stale`` -- a newer revision exists (or the record changed); the cited
     revision is still retained and its span verifies, so content is returned;
   * ``unavailable`` -- the bytes are gone (deleted/tombstoned, digest-only, or
     failing the digest check); no content;
   * ``ambiguous`` -- the locator matches several positions (e.g. a symbol
     defined twice); no content;
   * ``unsupported`` -- no usable locator (PDF without an extractor, a range
     outside the revision, binary media); no content.

These states describe evidence resolution, not the truth of any claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..contracts import (
    CodeLocator,
    Locator,
    OpenResult,
    QueryContext,
    SourceRecord,
    SourceState,
    SourceStatus,
    VerificationState,
    derive_evidence_id,
    sha256_digest,
)
from ..errors import DeniedError, InvalidInputError, UnknownScopeError
from ..scope import DEFAULT_PROFILE_ID, RecordView, record_in_scope
from .extract import is_text_media, slice_locator
from .store import EvidenceStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resolution:
    """An :class:`OpenResult` plus what the contract model does not carry."""

    result: OpenResult
    record: SourceRecord
    reason: str | None = None
    evidence_id: str | None = None
    excerpt_digest: str | None = None
    current_revision_id: str | None = None

    @property
    def verification(self) -> VerificationState:
        return self.result.verification


def check_scope(
    evidence: EvidenceStore,
    context: QueryContext,
    record: SourceRecord,
    *,
    configured_profile: str = DEFAULT_PROFILE_ID,
) -> None:
    """Raise ``denied`` unless ``record`` is inside the caller's resolved scope."""
    scope = evidence.store.resolve(context, configured_profile=configured_profile)
    view = RecordView(
        source_id=record.source_id,
        record_id=record.record_id,
        project_id=record.scope.project_id,
        research_type=record.research_type,
        tags=frozenset(record.tags),
        collection_ids=frozenset(record.collection_ids),
    )
    if record.scope.workspace_id != scope.workspace_id or not record_in_scope(view, scope):
        raise DeniedError(
            "the evidence is outside the requested scope",
            details={"source_id": record.source_id, "record_id": record.record_id},
        )


def open_evidence(
    evidence: EvidenceStore,
    context: QueryContext,
    *,
    source_id: str,
    record_id: str,
    revision_id: str,
    locator: Locator | None = None,
    configured_profile: str = DEFAULT_PROFILE_ID,
) -> Resolution:
    """Open the exact cited revision (and span) after rechecking scope; see the module doc."""
    with evidence.snapshot():
        state = evidence.record_state(source_id, record_id)
        check_scope(evidence, context, state.record, configured_profile=configured_profile)
        info = evidence.get_revision(revision_id)
        if info is None or (
            info.revision.source_id != source_id or info.revision.record_id != record_id
        ):
            raise UnknownScopeError(
                f"unknown revision {revision_id!r} of {source_id}/{record_id}",
                details={"kind": "revision", "id": revision_id},
            )
        revision = info.revision
        provenance = evidence.derivations_producing(revision_id)
        data = evidence.revision_bytes(revision_id) if revision.bytes_retained else None

    def build(
        verification: VerificationState,
        *,
        content: str | None = None,
        reason: str | None = None,
        evidence_id: str | None = None,
        excerpt_digest: str | None = None,
    ) -> Resolution:
        result = OpenResult(
            source_id=source_id,
            record_id=record_id,
            revision_id=revision_id,
            scope=state.record.scope,
            media_type=revision.media_type,
            content=content,
            locator=locator,
            verification=verification,
            digest=revision.digest,
            provenance_id=provenance[0].run_id if provenance else None,
            source_status=SourceStatus(source_id=source_id, state=SourceState.OK),
        )
        return Resolution(
            result,
            state.record,
            reason,
            evidence_id,
            excerpt_digest,
            state.current_revision_id,
        )

    unavailable = VerificationState.UNAVAILABLE
    if not revision.bytes_retained:
        return build(unavailable, reason=revision.unavailable_reason or "bytes are not retained")
    if data is None:
        _logger.warning("evidence: retained bytes of %s are missing", revision_id)
        return build(unavailable, reason="the retained bytes are missing")
    if sha256_digest(data) != revision.digest:
        _logger.error("evidence: retained bytes of %s fail the digest check", revision_id)
        return build(unavailable, reason="the retained bytes fail the digest check")

    if locator is None:
        if not is_text_media(revision.media_type):
            return build(
                VerificationState.UNSUPPORTED,
                reason=f"{revision.media_type} has no text content",
            )
        excerpt = data
    elif isinstance(locator, CodeLocator) and not _code_path_matches(locator, state.record):
        return build(
            VerificationState.UNSUPPORTED,
            reason=f"the locator's path {locator.path!r} is not this record's path",
        )
    else:
        sliced = slice_locator(
            data,
            locator,
            media_type=revision.media_type,
            upstream_version=revision.upstream_version,
        )
        if sliced.status == "ambiguous":
            return build(VerificationState.AMBIGUOUS, reason=sliced.reason)
        if sliced.excerpt is None:
            return build(VerificationState.UNSUPPORTED, reason=sliced.reason)
        excerpt = sliced.excerpt
    if len(excerpt) > context.byte_budget:
        raise InvalidInputError(
            f"the content has {len(excerpt)} bytes, more than the byte_budget"
            f" {context.byte_budget}; cite a narrower locator or raise byte_budget",
            details={"reason": "byte_budget", "bytes": len(excerpt)},
        )
    try:
        text = excerpt.decode("utf-8")
    except UnicodeDecodeError:
        return build(VerificationState.UNSUPPORTED, reason="the content is not valid UTF-8")
    digest = sha256_digest(excerpt)
    evidence_id = derive_evidence_id(revision_id, locator, digest) if locator is not None else None
    current = state.current_revision_id == revision_id and state.deleted_at is None
    if current:
        return build(
            VerificationState.VERIFIED, content=text, evidence_id=evidence_id, excerpt_digest=digest
        )
    return build(
        VerificationState.STALE,
        content=text,
        reason="a newer revision of the record exists; this is the cited revision",
        evidence_id=evidence_id,
        excerpt_digest=digest,
    )


def _code_path_matches(locator: CodeLocator, record: SourceRecord) -> bool:
    """A code locator's repository-relative path names this record (path, alias or suffix)."""
    paths = [p for p in (record.logical_path, *record.aliases) if p]
    return any(p == locator.path or p.endswith("/" + locator.path) for p in paths)


def open_span(
    evidence: EvidenceStore,
    context: QueryContext,
    evidence_id: str,
    *,
    configured_profile: str = DEFAULT_PROFILE_ID,
) -> Resolution:
    """Open a stored evidence span by id; its excerpt digest must still match."""
    info = evidence.get_span(evidence_id)
    if info is None:
        raise UnknownScopeError(
            f"unknown evidence {evidence_id!r}", details={"kind": "evidence", "id": evidence_id}
        )
    resolution = open_evidence(
        evidence,
        context,
        source_id=info.source_id,
        record_id=info.record_id,
        revision_id=info.span.revision_id,
        locator=info.span.locator,
        configured_profile=configured_profile,
    )
    if (
        resolution.excerpt_digest is not None
        and resolution.excerpt_digest != info.span.excerpt_digest
    ):
        return Resolution(
            resolution.result.model_copy(
                update={"verification": VerificationState.UNSUPPORTED, "content": None}
            ),
            resolution.record,
            "the span's excerpt digest no longer matches its revision",
            evidence_id,
            None,
            resolution.current_revision_id,
        )
    if resolution.evidence_id is None:
        return Resolution(
            resolution.result,
            resolution.record,
            resolution.reason,
            evidence_id,
            info.span.excerpt_digest,
            resolution.current_revision_id,
        )
    return resolution


__all__ = ["Resolution", "check_scope", "open_evidence", "open_span"]
