"""Versioned public contracts of scoped knowledge, evidence and adapters (schema version 1).

These Pydantic models are the stable boundary between the HTTP/CLI/MCP
bindings, the metadata store, the retrieval broker, source adapters, the
evidence resolver and modules. They are pure data: importing this module does
no I/O. The JSON Schema of every public model is pinned in
``engine/tests/snapshots/knowledge_contracts.json``
(:func:`contract_json_schema`); a change there is a contract change.

Rules every model follows:

* each top-level model carries ``schema_version`` (currently ``1``); a
  payload with another version is rejected, never reinterpreted;
* models are immutable and reject unknown fields (``extra="forbid"``);
* identities are stable strings, never SQLite row ids or chunk ordinals
  (ADR 0007): a record is ``(source_id, record_id)``, a revision is
  content-addressed within its record (:func:`derive_revision_id`), an
  evidence span is addressed by revision + locator + excerpt digest
  (:func:`derive_evidence_id`);
* every range in a locator is **half-open** ``[start, end)``. Text offsets are
  **UTF-8 byte offsets** into the exact retained revision bytes; transcript
  times are integer milliseconds; code lines are 1-based line numbers with an
  exclusive end (a single line ``n`` is ``start_line=n, end_line=n+1``);
  PDF pages are 1-based and a bounding box is in PDF points with the origin at
  the bottom-left of the page.

Filter semantics of :class:`QueryContext` (ADR 0006) are implemented by
:mod:`knowledge_engine.knowledge.scope`.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

#: The version every contract model in this module carries.
CONTRACT_VERSION: Literal[1] = 1

#: Upper bound on the ids of one selection list (keeps SQL parameter counts bounded).
MAX_SELECTION = 200

SchemaVersion = Annotated[
    Literal[1],
    Field(description="Contract schema version; payloads of another version are rejected."),
]

# ── Identifiers ──────────────────────────────────────────────────────

#: Workspace, project, collection and profile ids: lowercase, opaque, stable.
OPAQUE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
#: Namespaced source id ``<namespace>:<name>``, e.g. ``corpus:base``.
SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}:[a-z0-9][a-z0-9._-]{0,95}$"
#: Source-owned record id (no ``/`` so it can be a URL path segment).
RECORD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$"
#: Content-addressed revision id (see :func:`derive_revision_id`).
REVISION_ID_PATTERN = r"^rev_[0-9a-f]{32}$"
#: Evidence span id (see :func:`derive_evidence_id`).
EVIDENCE_ID_PATTERN = r"^ev_[0-9a-f]{32}$"
#: ``sha256:<64 lowercase hex>``.
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
#: Git commit (SHA-1 or SHA-256 object name).
COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
#: A normalized tag (see :func:`normalize_tag`).
TAG_PATTERN = r"^[a-z0-9][a-z0-9._:/+-]{0,63}$"

WorkspaceId = Annotated[str, StringConstraints(pattern=OPAQUE_ID_PATTERN)]
ProjectId = Annotated[str, StringConstraints(pattern=OPAQUE_ID_PATTERN)]
CollectionId = Annotated[str, StringConstraints(pattern=OPAQUE_ID_PATTERN)]
ProfileId = Annotated[str, StringConstraints(pattern=OPAQUE_ID_PATTERN)]
SourceId = Annotated[str, StringConstraints(pattern=SOURCE_ID_PATTERN)]
RecordId = Annotated[str, StringConstraints(pattern=RECORD_ID_PATTERN)]
RevisionId = Annotated[str, StringConstraints(pattern=REVISION_ID_PATTERN)]
EvidenceId = Annotated[str, StringConstraints(pattern=EVIDENCE_ID_PATTERN)]
Digest = Annotated[str, StringConstraints(pattern=DIGEST_PATTERN)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(max_length=2000)]

_TAG_RE = re.compile(TAG_PATTERN)
_TAG_SPACE_RE = re.compile(r"\s+")


def normalize_tag(value: str) -> str:
    """The canonical form of a tag: NFKC, trimmed, case-folded, inner spaces as ``-``.

    Raises ``ValueError`` when the result is empty or contains characters
    outside ``[a-z0-9._:/+-]`` (max 64 characters, starting alphanumeric).
    Tags are labels for discovery, never authorization.
    """
    if not isinstance(value, str):
        raise ValueError("a tag must be a string")
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    text = _TAG_SPACE_RE.sub("-", text)
    if not _TAG_RE.fullmatch(text):
        raise ValueError(
            f"invalid tag {value!r}: use 1-64 characters from a-z 0-9 . _ : / + - "
            "starting with a letter or digit"
        )
    return text


def normalize_tags(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize, de-duplicate and sort tags (tag sets have no order)."""
    return tuple(sorted({normalize_tag(v) for v in values}))


def _unique(values: Any) -> Any:
    """De-duplicate a selection list while keeping the caller's order."""
    if isinstance(values, (list, tuple)):
        seen: dict[Any, None] = {}
        for value in values:
            seen.setdefault(value, None)
        return tuple(seen)
    return values


def _tags_or_none(values: Any) -> Any:
    if values is None:
        return None
    if isinstance(values, str):
        raise ValueError("tags must be a list of strings")
    return normalize_tags(values)


def _sorted_set(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


#: A set of collection ids, serialized sorted (membership has no order).
CollectionIdSet = Annotated[tuple[CollectionId, ...], AfterValidator(_sorted_set)]


def sha256_digest(data: bytes) -> str:
    """``sha256:<hex>`` of ``data``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _id_hash(*parts: str) -> str:
    joined = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:32]


def derive_revision_id(source_id: str, record_id: str, digest: str) -> str:
    """Content-addressed revision id: the same bytes of the same record are one revision.

    Two records with identical bytes keep distinct revisions (the record
    identity is part of the hash); reindexing never changes the id.
    """
    if not re.fullmatch(DIGEST_PATTERN, digest):
        raise ValueError(f"invalid digest {digest!r}")
    return "rev_" + _id_hash("revision", source_id, record_id, digest)


def canonical_json(value: Any) -> str:
    """Deterministic JSON (sorted keys, no whitespace) used for identity hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── Controlled vocabularies ──────────────────────────────────────────


class ResearchType(StrEnum):
    """Controlled content classification. Unknown values fail validation."""

    REFERENCE = "reference"
    PAPER = "paper"
    ARTICLE = "article"
    TRANSCRIPT = "transcript"
    CODE = "code"
    NOTE = "note"
    DATASET = "dataset"
    SYNTHESIS = "synthesis"
    OTHER = "other"


class CurationStatus(StrEnum):
    """Review maturity. Never inferred from model output."""

    UNCLASSIFIED = "unclassified"
    INBOX = "inbox"
    REVIEWED = "reviewed"
    ARCHIVED = "archived"


class VerificationState(StrEnum):
    """Outcome of resolving evidence; says nothing about the truth of a claim."""

    VERIFIED = "verified"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class SourceState(StrEnum):
    """Health of one source for one request (or a health probe)."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ERROR = "error"


#: Source states that mean "this source contributed nothing".
FAILED_SOURCE_STATES = frozenset({SourceState.UNAVAILABLE, SourceState.TIMEOUT, SourceState.ERROR})

SearchMode = Literal["fts", "vector", "hybrid"]
SEARCH_MODES: tuple[str, ...] = ("fts", "vector", "hybrid")


class ContractModel(BaseModel):
    """Base of every contract model: immutable, unknown fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


# ── Locators (tagged union) ──────────────────────────────────────────


class TextLocator(ContractModel):
    """A span of a text revision: UTF-8 byte offsets, half-open ``[start_byte, end_byte)``."""

    kind: Literal["text"] = "text"
    heading: str | None = Field(
        default=None, max_length=500, description="Nearest heading text, informational only."
    )
    start_byte: int = Field(ge=0, description="Inclusive UTF-8 byte offset into the revision.")
    end_byte: int = Field(ge=0, description="Exclusive UTF-8 byte offset into the revision.")

    @model_validator(mode="after")
    def _ordered(self) -> TextLocator:
        if self.end_byte < self.start_byte:
            raise ValueError("end_byte must be >= start_byte (half-open range)")
        return self


class BoundingBox(ContractModel):
    """A rectangle in PDF points, origin at the bottom-left of the page."""

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _ordered(self) -> BoundingBox:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bounding box needs x0 <= x1 and y0 <= y1")
        return self


class PdfLocator(ContractModel):
    """A 1-based PDF page, optionally narrowed to a bounding box."""

    kind: Literal["pdf"] = "pdf"
    page: int = Field(ge=1, description="1-based page number.")
    bbox: BoundingBox | None = None


class TranscriptLocator(ContractModel):
    """A transcript segment: milliseconds, half-open ``[start_ms, end_ms)``."""

    kind: Literal["transcript"] = "transcript"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str | None = Field(default=None, max_length=200, description="Only when known.")

    @model_validator(mode="after")
    def _ordered(self) -> TranscriptLocator:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be >= start_ms (half-open range)")
        return self


class CodeLocator(ContractModel):
    """Code in a repository at an exact commit, or at a snapshot digest when dirty.

    Exactly one of ``commit`` and ``snapshot_digest`` is set: a dirty working
    tree is never represented as a clean commit. Lines are 1-based with an
    exclusive ``end_line``.
    """

    kind: Literal["code"] = "code"
    repository_id: str = Field(min_length=1, max_length=200)
    commit: Annotated[str, StringConstraints(pattern=COMMIT_PATTERN)] | None = None
    snapshot_digest: Digest | None = None
    path: str = Field(min_length=1, max_length=1024, description="Repository-relative POSIX path.")
    symbol: str | None = Field(default=None, max_length=500, description="Qualified symbol name.")
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> CodeLocator:
        if (self.commit is None) == (self.snapshot_digest is None):
            raise ValueError("exactly one of commit and snapshot_digest is required")
        if self.path.startswith("/") or "\\" in self.path or ".." in self.path.split("/"):
            raise ValueError("path must be a repository-relative POSIX path")
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line are given together")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line <= self.start_line
        ):
            raise ValueError("end_line is exclusive and must be > start_line")
        return self


class UnsupportedLocator(ContractModel):
    """No usable position could be recovered; never replaced by an invented one."""

    kind: Literal["unsupported"] = "unsupported"
    reason: str = Field(min_length=1, max_length=500)


Locator = Annotated[
    TextLocator | PdfLocator | TranscriptLocator | CodeLocator | UnsupportedLocator,
    Field(discriminator="kind"),
]


def derive_evidence_id(revision_id: str, locator: BaseModel, excerpt_digest: str) -> str:
    """Stable evidence identity: revision + canonical locator + excerpt digest."""
    return "ev_" + _id_hash(
        "evidence", revision_id, canonical_json(locator.model_dump(mode="json")), excerpt_digest
    )


# ── Scope and queries ────────────────────────────────────────────────


class TrustedPrincipal(ContractModel):
    """Who is asking, as established by a trusted boundary (never by the caller's JSON)."""

    principal_id: str = Field(min_length=1, max_length=200)
    allowed_workspaces: tuple[WorkspaceId, ...] = ()


class QueryContext(ContractModel):
    """Scope and budget of one query.

    Selection semantics (ADR 0006):

    * ``None`` (omitted) takes the dimension from the named profile
      (``profile_id``, default: the configured profile);
    * an explicit empty ``source_ids`` / ``collection_ids`` / ``research_types``
      selects **no** sources and returns nothing;
    * values within one list are ORed; ``tags`` are ANDed; dimensions are ANDed;
      an explicit empty ``tags`` list means "no tag constraint" (and overrides
      the profile's tags);
    * unknown ids are ``unknown_scope`` errors, never an empty or a global query;
    * with ``project_id`` only that project's records match, plus
      workspace-wide records (no project) when ``include_workspace`` is true.

    ``principal`` is set by trusted code only; :func:`parse_query_context`
    rejects it in caller-supplied payloads.
    """

    schema_version: SchemaVersion = CONTRACT_VERSION
    workspace_id: WorkspaceId
    project_id: ProjectId | None = None
    profile_id: ProfileId | None = None
    source_ids: Annotated[
        tuple[SourceId, ...] | None, BeforeValidator(_unique), Field(max_length=MAX_SELECTION)
    ] = None
    collection_ids: Annotated[
        tuple[CollectionId, ...] | None, BeforeValidator(_unique), Field(max_length=MAX_SELECTION)
    ] = None
    research_types: Annotated[
        tuple[ResearchType, ...] | None, BeforeValidator(_unique), Field(max_length=MAX_SELECTION)
    ] = None
    tags: Annotated[
        tuple[str, ...] | None, BeforeValidator(_tags_or_none), Field(max_length=MAX_SELECTION)
    ] = None
    include_workspace: bool = False
    mode: SearchMode = "fts"
    limit: int = Field(default=10, ge=1, le=100)
    timeout_ms: int = Field(default=10_000, ge=1, le=120_000)
    byte_budget: int = Field(default=1_048_576, ge=1, le=16_777_216)
    principal: TrustedPrincipal | None = Field(
        default=None, description="Set by a trusted boundary only; rejected from callers."
    )


def parse_query_context(payload: Any) -> QueryContext:
    """Validate a caller-supplied query context (HTTP body, MCP arguments, CLI flags).

    A caller cannot grant itself scope: a ``principal`` in the payload is
    rejected with ``invalid_input``; trusted code attaches one with
    ``context.model_copy(update={"principal": ...})``. Validation failures
    raise :class:`~knowledge_engine.knowledge.errors.InvalidInputError`.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from .errors import InvalidInputError  # noqa: PLC0415

    if not isinstance(payload, dict):
        raise InvalidInputError("a query context must be a JSON object")
    if "principal" in payload:
        raise InvalidInputError(
            "principal cannot be supplied by the caller", details={"field": "principal"}
        )
    try:
        return QueryContext.model_validate(payload)
    except ValidationError as exc:
        raise InvalidInputError(
            "invalid query context", details={"errors": validation_messages(exc)}
        ) from exc


def validation_messages(exc: Any) -> list[str]:
    """``"field.path: message"`` strings of a pydantic ``ValidationError`` (JSON-safe)."""
    out: list[str] = []
    for err in exc.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in err.get("loc", ()))
        out.append(f"{where}: {err.get('msg', 'invalid')}" if where else str(err.get("msg")))
    return out


class ScopeProfile(ContractModel):
    """A named default selection used for every dimension a query omits."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    workspace_id: WorkspaceId
    profile_id: ProfileId
    description: Description = ""
    source_ids: Annotated[tuple[SourceId, ...] | None, BeforeValidator(_unique)] = None
    collection_ids: Annotated[tuple[CollectionId, ...] | None, BeforeValidator(_unique)] = None
    research_types: Annotated[tuple[ResearchType, ...] | None, BeforeValidator(_unique)] = None
    tags: Annotated[tuple[str, ...], BeforeValidator(_tags_or_none)] = ()


class EffectiveScope(ContractModel):
    """The resolved scope a request actually ran with (returned to the caller)."""

    workspace_id: WorkspaceId
    project_id: ProjectId | None = None
    include_workspace: bool = False
    profile_id: ProfileId
    source_ids: tuple[SourceId, ...] = Field(description="Concrete sources searched (sorted).")
    collection_ids: tuple[CollectionId, ...] | None = Field(
        default=None, description="None: no collection constraint."
    )
    research_types: tuple[ResearchType, ...] | None = Field(
        default=None, description="None: no research-type constraint."
    )
    tags: tuple[str, ...] = Field(default=(), description="Every tag must be present (AND).")
    from_profile: tuple[str, ...] = Field(
        default=(), description="Dimensions taken from the profile because the query omitted them."
    )
    empty: bool = Field(
        default=False, description="An explicit empty selection: nothing can match."
    )


class RecordScope(ContractModel):
    """Where a record lives: its workspace and optional project."""

    workspace_id: WorkspaceId
    project_id: ProjectId | None = None


class ErrorInfo(ContractModel):
    """The wire form of a :class:`~knowledge_engine.knowledge.errors.KnowledgeError`."""

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class SourceStatus(ContractModel):
    """Per-source outcome of a request (or of a health probe)."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    state: SourceState
    error: ErrorInfo | None = None
    hit_count: int = Field(default=0, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _error_matches_state(self) -> SourceStatus:
        if self.state in FAILED_SOURCE_STATES and self.error is None:
            raise ValueError(f"a source in state {self.state.value!r} must carry an error")
        if self.state in FAILED_SOURCE_STATES and self.hit_count:
            raise ValueError("a failed source contributes no hits")
        return self


class RankingInfo(ContractModel):
    """Native rank/score provenance; raw scores of different stores are never compared."""

    native_rank: int = Field(ge=1, description="1-based rank within the source's own result.")
    native_score: float | None = None
    score_kind: Literal["bm25", "cosine", "rrf", "other"] = "other"
    fused_rank: int | None = Field(default=None, ge=1)
    fused_score: float | None = None


class SearchHit(ContractModel):
    """One normalized hit. ``revision_id`` is absent until the owning store records revisions."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    record_id: RecordId
    revision_id: RevisionId | None = None
    scope: RecordScope
    collection_ids: CollectionIdSet = ()
    research_type: ResearchType = ResearchType.OTHER
    title: str = Field(default="", max_length=1000)
    locator: Locator | None = None
    excerpt: str = Field(default="", max_length=20_000)
    provenance_id: str | None = Field(default=None, max_length=200)
    ranking: RankingInfo

    @property
    def identity(self) -> tuple[str, str, str]:
        """Deduplication key: source-owned record + revision (never the text)."""
        return (self.source_id, self.record_id, self.revision_id or "")


class SearchEnvelope(ContractModel):
    """The result of one broker search.

    ``partial`` is true exactly when some selected source did not answer
    ``ok``. An envelope in which every source failed is invalid: that request
    fails with ``unavailable`` instead. No matches with healthy sources is a
    valid, empty envelope.
    """

    schema_version: SchemaVersion = CONTRACT_VERSION
    contract_version: Literal[1] = CONTRACT_VERSION
    hits: tuple[SearchHit, ...] = ()
    effective_scope: EffectiveScope
    source_status: tuple[SourceStatus, ...] = ()
    partial: bool = False
    warnings: tuple[str, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _consistent(self) -> SearchEnvelope:
        states = [status.state for status in self.source_status]
        degraded = any(state != SourceState.OK for state in states)
        if self.partial != degraded:
            raise ValueError("partial must be true exactly when a source is not ok")
        if states and all(state in FAILED_SOURCE_STATES for state in states):
            raise ValueError("every selected source failed: raise UnavailableError instead")
        searched = {status.source_id for status in self.source_status}
        stray = sorted({hit.source_id for hit in self.hits} - searched) if searched else []
        if stray:
            raise ValueError(f"hits from sources without a status: {stray}")
        return self


class OpenResult(ContractModel):
    """The exact revision of a record (or why it cannot be served)."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    record_id: RecordId
    revision_id: RevisionId
    scope: RecordScope
    media_type: str = Field(min_length=1, max_length=200)
    content: str | None = Field(default=None, description="Text content when available.")
    locator: Locator | None = None
    verification: VerificationState
    digest: Digest | None = None
    provenance_id: str | None = Field(default=None, max_length=200)
    source_status: SourceStatus

    @model_validator(mode="after")
    def _consistent(self) -> OpenResult:
        if self.verification == VerificationState.VERIFIED and (
            self.digest is None or self.content is None
        ):
            raise ValueError("a verified open result carries its content and digest")
        if self.verification == VerificationState.UNAVAILABLE and self.content is not None:
            raise ValueError("unavailable bytes cannot carry content")
        return self


# ── Organization records (owned by the metadata store, knowledge.db) ─


class Workspace(ContractModel):
    """An explicit knowledge boundary. The id is stable; the display name may change."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    workspace_id: WorkspaceId
    display_name: DisplayName
    description: Description = ""
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Project(ContractModel):
    """An optional narrower context. Display names are not unique; the id is the identity."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    project_id: ProjectId
    workspace_id: WorkspaceId
    display_name: DisplayName
    description: Description = ""
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Source(ContractModel):
    """A system that owns records (the base corpus, project context, an external MCP ...)."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    workspace_id: WorkspaceId
    kind: str = Field(min_length=1, max_length=64)
    display_name: DisplayName
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Collection(ContractModel):
    """A curated grouping with many-to-many membership; organization, not permission."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    collection_id: CollectionId
    workspace_id: WorkspaceId
    project_id: ProjectId | None = None
    display_name: DisplayName
    description: Description = ""
    member_count: int = Field(default=0, ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime


# ── Records (store schemas; P3 builds the revision/evidence store) ───


class Bibliographic(ContractModel):
    """Recorded bibliographic metadata. Unknown fields stay absent, never invented."""

    authors: tuple[str, ...] = ()
    published: str | None = Field(
        default=None, max_length=40, description="ISO 8601 date or partial date as recorded."
    )
    publisher: str | None = Field(default=None, max_length=500)
    container_title: str | None = Field(default=None, max_length=500)
    identifiers: dict[str, str] = Field(
        default_factory=dict, description="e.g. {'doi': ..., 'isbn': ...}"
    )


class RetentionMode(StrEnum):
    """What the owning store may keep of a record's bytes (ADR 0008)."""

    RETAIN = "retain"
    DIGEST_ONLY = "digest_only"
    TOMBSTONE = "tombstone"


class RightsInfo(ContractModel):
    """Rights and retention metadata as recorded by the operator or the source."""

    license: str | None = Field(default=None, max_length=200)
    retention: RetentionMode = RetentionMode.RETAIN
    notes: str | None = Field(default=None, max_length=2000)


class SourceRecord(ContractModel):
    """A record owned by one source. Identity: ``(source_id, record_id)``."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    source_id: SourceId
    record_id: RecordId
    scope: RecordScope
    canonical_uri: str | None = Field(default=None, max_length=2048)
    logical_path: str | None = Field(
        default=None, max_length=1024, description="Source-relative POSIX path."
    )
    title: str = Field(default="", max_length=1000)
    research_type: ResearchType = ResearchType.OTHER
    curation_status: CurationStatus = CurationStatus.UNCLASSIFIED
    tags: Annotated[tuple[str, ...], BeforeValidator(_tags_or_none)] = ()
    collection_ids: CollectionIdSet = ()
    aliases: tuple[str, ...] = Field(default=(), description="Former logical paths / URIs.")
    bibliographic: Bibliographic = Field(default_factory=Bibliographic)
    rights: RightsInfo = Field(default_factory=RightsInfo)

    @model_validator(mode="after")
    def _addressable(self) -> SourceRecord:
        if self.canonical_uri is None and self.logical_path is None:
            raise ValueError("a record needs a canonical_uri or a logical_path")
        if self.logical_path is not None and (
            self.logical_path.startswith("/") or ".." in self.logical_path.split("/")
        ):
            raise ValueError("logical_path must be a relative POSIX path")
        return self


class Revision(ContractModel):
    """Immutable bytes of a record at one point in time."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    revision_id: RevisionId
    source_id: SourceId
    record_id: RecordId
    digest: Digest = Field(description="SHA-256 of the retained bytes.")
    byte_length: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)
    observed_at: AwareDatetime
    upstream_version: str | None = Field(default=None, max_length=200)
    upstream_date: str | None = Field(default=None, max_length=40)
    bytes_retained: bool = True
    unavailable_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> Revision:
        expected = derive_revision_id(self.source_id, self.record_id, self.digest)
        if self.revision_id != expected:
            raise ValueError(
                "revision_id does not match derive_revision_id(source, record, digest)"
            )
        if not self.bytes_retained and not self.unavailable_reason:
            raise ValueError("a revision without retained bytes records why")
        return self


class ExtractionInfo(ContractModel):
    """How an evidence span was extracted."""

    method: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)


class EvidenceSpan(ContractModel):
    """An exact excerpt of one revision."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    evidence_id: EvidenceId
    revision_id: RevisionId
    locator: Locator
    excerpt_digest: Digest
    extraction: ExtractionInfo

    @model_validator(mode="after")
    def _consistent(self) -> EvidenceSpan:
        expected = derive_evidence_id(self.revision_id, self.locator, self.excerpt_digest)
        if self.evidence_id != expected:
            raise ValueError("evidence_id does not match derive_evidence_id(...)")
        return self


class Derivation(ContractModel):
    """Lineage of revisions produced by a module run."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    run_id: str = Field(min_length=1, max_length=200)
    module_id: str = Field(min_length=1, max_length=200)
    module_version: str = Field(min_length=1, max_length=100)
    parent_revision_ids: tuple[RevisionId, ...] = ()
    parameters_digest: Digest
    output_revision_ids: tuple[RevisionId, ...] = Field(min_length=1)


class CitationExport(ContractModel):
    """Bibliographic fields for export; missing values stay ``None``."""

    title: str | None = None
    authors: tuple[str, ...] = ()
    published: str | None = None
    uri: str | None = None
    publisher: str | None = None


class Citation(ContractModel):
    """A citation of one evidence span and its last verification state."""

    schema_version: SchemaVersion = CONTRACT_VERSION
    evidence_id: EvidenceId
    source_id: SourceId
    record_id: RecordId
    revision_id: RevisionId
    locator: Locator
    export: CitationExport = Field(default_factory=CitationExport)
    verification: VerificationState


# ── JSON Schema export ───────────────────────────────────────────────

#: The public contract models, in documentation order.
PUBLIC_MODELS: tuple[type[BaseModel], ...] = (
    Workspace,
    Project,
    Source,
    Collection,
    QueryContext,
    ScopeProfile,
    EffectiveScope,
    SourceStatus,
    SearchHit,
    SearchEnvelope,
    OpenResult,
    SourceRecord,
    Revision,
    EvidenceSpan,
    Derivation,
    Citation,
    TextLocator,
    PdfLocator,
    TranscriptLocator,
    CodeLocator,
    UnsupportedLocator,
)


def contract_json_schema() -> dict[str, Any]:
    """JSON Schema of every public model plus the controlled vocabularies (deterministic)."""
    from .adapter import AdapterCapabilities, AdapterSearchResult  # noqa: PLC0415 - avoid cycle

    models: dict[str, Any] = {}
    exported: list[type[BaseModel]] = [*PUBLIC_MODELS, AdapterCapabilities, AdapterSearchResult]
    for model in exported:
        models[model.__name__] = model.model_json_schema(mode="validation")
    from .errors import ERROR_CODES  # noqa: PLC0415

    return {
        "contract_version": CONTRACT_VERSION,
        "vocabularies": {
            "ResearchType": [item.value for item in ResearchType],
            "CurationStatus": [item.value for item in CurationStatus],
            "VerificationState": [item.value for item in VerificationState],
            "SourceState": [item.value for item in SourceState],
            "SearchMode": list(SEARCH_MODES),
            "ErrorCode": list(ERROR_CODES),
        },
        "models": models,
    }


__all__ = [
    "CONTRACT_VERSION",
    "FAILED_SOURCE_STATES",
    "MAX_SELECTION",
    "PUBLIC_MODELS",
    "SEARCH_MODES",
    "Bibliographic",
    "BoundingBox",
    "Citation",
    "CitationExport",
    "CodeLocator",
    "Collection",
    "CollectionId",
    "CollectionIdSet",
    "ContractModel",
    "CurationStatus",
    "Derivation",
    "Digest",
    "EffectiveScope",
    "ErrorInfo",
    "EvidenceSpan",
    "ExtractionInfo",
    "Locator",
    "OpenResult",
    "PdfLocator",
    "ProfileId",
    "Project",
    "ProjectId",
    "QueryContext",
    "RankingInfo",
    "RecordId",
    "RecordScope",
    "ResearchType",
    "RetentionMode",
    "Revision",
    "RevisionId",
    "RightsInfo",
    "ScopeProfile",
    "SearchEnvelope",
    "SearchHit",
    "SearchMode",
    "Source",
    "SourceId",
    "SourceRecord",
    "SourceState",
    "SourceStatus",
    "TextLocator",
    "TranscriptLocator",
    "TrustedPrincipal",
    "UnsupportedLocator",
    "VerificationState",
    "Workspace",
    "WorkspaceId",
    "canonical_json",
    "contract_json_schema",
    "derive_evidence_id",
    "derive_revision_id",
    "normalize_tag",
    "normalize_tags",
    "parse_query_context",
    "sha256_digest",
    "validation_messages",
]
