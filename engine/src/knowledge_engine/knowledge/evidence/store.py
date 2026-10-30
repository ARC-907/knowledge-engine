"""The evidence repository in ``knowledge.db`` and the host-side ingestion API (ADR 0012).

Tables (migration ``002_evidence.sql``): revisions, retained bytes, record
aliases, evidence spans, derivations, tombstones, idempotent ingestion
requests and the cross-store outbox; plus rights, bibliographic metadata, the
head revision and the deletion marker on ``records``.

:meth:`EvidenceStore.ingest` is the one write path for new bytes (the corpus
snapshot and, from P6, module artifacts):

* it validates size, media type, digest and scope (the source must exist; a
  new record is created workspace-wide and ``unclassified``);
* the record keeps its identity: a changed ``logical_path`` is a rename that
  records the old path as an alias; changed bytes are a new revision;
* the revision id is :func:`~knowledge_engine.knowledge.contracts.derive_revision_id`
  -- identical bytes in two records are two revisions, and ingesting the same
  bytes again creates nothing (it only moves the head back if needed);
* bytes are retained unless the record's retention is ``digest_only`` or the
  revision exceeds the policy's ``max_bytes`` -- then the digest, an
  external locator and the reason are kept;
* a request with an ``idempotency_key`` is replayed when key *and* inputs
  match and refused when the same key arrives with other inputs;
* everything above, the evidence spans, the derivation lineage and an outbox
  event commit in **one** transaction. Index generations in other stores
  consume the outbox idempotently (:meth:`EvidenceStore.pending_outbox`,
  :meth:`EvidenceStore.ack_outbox`) instead of pretending to share a
  cross-database transaction.

:meth:`EvidenceStore.delete_record` follows :class:`EvidencePolicy.on_delete`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from ..contracts import (
    Bibliographic,
    Derivation,
    Digest,
    EvidenceSpan,
    ExtractionInfo,
    Locator,
    RecordId,
    RetentionMode,
    Revision,
    RevisionId,
    RightsInfo,
    SourceId,
    SourceRecord,
    canonical_json,
    derive_evidence_id,
    derive_revision_id,
    sha256_digest,
    validation_messages,
)
from ..errors import DeniedError, InvalidInputError, UnknownScopeError
from ..store import KnowledgeStore
from .extract import ExtractedSpan, default_spans, slice_locator
from .policy import EvidencePolicy

_logger = logging.getLogger(__name__)

_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*(?:;[ -~]{1,100})?$")
_LOCATOR = TypeAdapter(Locator)
#: The ``knowledge.db`` schema version that adds the evidence tables.
EVIDENCE_SCHEMA_VERSION = 2
#: Largest outbox page.
MAX_OUTBOX_PAGE = 1000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpanInput(_Model):
    """A span the producer declares; the host computes and checks its excerpt."""

    locator: Locator
    extraction: ExtractionInfo


class DerivationInput(_Model):
    """Lineage of the ingested revision: produced by ``module_id`` in run ``run_id``."""

    run_id: str = Field(min_length=1, max_length=200)
    module_id: str = Field(min_length=1, max_length=200)
    module_version: str = Field(min_length=1, max_length=100)
    parameters_digest: Digest
    parent_revision_ids: tuple[RevisionId, ...] = ()


class IngestRequest(_Model):
    """One artifact to record as a revision of ``(source_id, record_id)``.

    Give ``content`` (the exact bytes), or -- when bytes may not be retained --
    ``digest`` + ``byte_length`` + ``external_locator`` + ``unavailable_reason``.
    """

    source_id: SourceId
    record_id: RecordId
    logical_path: str | None = Field(default=None, max_length=1024)
    canonical_uri: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=1000)
    media_type: str = Field(min_length=3, max_length=200)
    content: bytes | None = None
    digest: Digest | None = None
    byte_length: int | None = Field(default=None, ge=0)
    external_locator: str | None = Field(default=None, max_length=2048)
    unavailable_reason: str | None = Field(default=None, max_length=500)
    observed_at: AwareDatetime | None = None
    upstream_version: str | None = Field(default=None, max_length=200)
    upstream_date: str | None = Field(default=None, max_length=40)
    bibliographic: Bibliographic | None = None
    rights: RightsInfo | None = None
    spans: tuple[SpanInput, ...] | None = Field(
        default=None, description="None: the default extraction for the media type."
    )
    derivation: DerivationInput | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _consistent(self) -> IngestRequest:
        if not _MEDIA_TYPE_RE.match(self.media_type):
            raise ValueError(f"invalid media type {self.media_type!r}")
        if self.logical_path is None and self.canonical_uri is None:
            raise ValueError("a record needs a logical_path or a canonical_uri")
        if self.logical_path is not None and (
            self.logical_path.startswith("/")
            or "\\" in self.logical_path
            or ".." in self.logical_path.split("/")
            or not self.logical_path
        ):
            raise ValueError("logical_path must be a relative POSIX path")
        if self.content is None:
            if self.digest is None or self.byte_length is None:
                raise ValueError("without content, digest and byte_length are required")
            if not self.unavailable_reason:
                raise ValueError("without content, unavailable_reason says why")
            if self.spans:
                raise ValueError("spans need the content to verify their excerpts")
        elif self.digest is not None and self.digest != sha256_digest(self.content):
            raise ValueError("digest does not match the content")
        return self

    def request_digest(self) -> str:
        """Digest of every input (bytes by digest) -- the idempotency check."""
        data = self.model_dump(mode="json", exclude={"content", "idempotency_key"})
        data["content_digest"] = sha256_digest(self.content) if self.content is not None else None
        return sha256_digest(canonical_json(data).encode("utf-8"))


class IngestResult(BaseModel):
    """What one ingestion did."""

    record: SourceRecord
    revision: Revision
    revision_created: bool
    record_created: bool
    head_changed: bool
    renamed_from: str | None = None
    restored: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    replayed: bool = False


@dataclass(frozen=True)
class _RecordUpdate:
    """Record fields an ingestion or a rename may change (``None``: unchanged)."""

    source_id: str
    record_id: str
    logical_path: str | None = None
    canonical_uri: str | None = None
    title: str | None = None
    bibliographic: Bibliographic | None = None
    rights: RightsInfo | None = None


@dataclass(frozen=True)
class RevisionInfo:
    """A stored revision plus the host-only fields the contract model omits."""

    revision: Revision
    external_locator: str | None
    deleted_at: str | None
    seq: int


@dataclass(frozen=True)
class RecordState:
    """Head and deletion marker of a record."""

    record: SourceRecord
    current_revision_id: str | None
    deleted_at: str | None


@dataclass(frozen=True)
class SpanInfo:
    span: EvidenceSpan
    source_id: str
    record_id: str


@dataclass(frozen=True)
class Tombstone:
    source_id: str
    record_id: str
    revision_id: str
    digest: str
    deleted_at: str
    reason: str


class EvidenceStore:
    """Evidence repository over the same ``knowledge.db`` as a :class:`KnowledgeStore`."""

    def __init__(self, store: KnowledgeStore, policy: EvidencePolicy | None = None) -> None:
        self.store = store
        self.policy = policy or EvidencePolicy()

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """One consistent read snapshot (reentrant)."""
        with self.store.read_transaction() as conn:
            yield conn

    # ── ingestion ────────────────────────────────────────────────

    def ingest(self, request: IngestRequest | dict[str, Any]) -> IngestResult:
        """Record an artifact as a revision (see the module docstring); atomic."""
        req = _parse(IngestRequest, request, "ingest request")
        content = req.content
        if content is not None and len(content) > self.policy.max_bytes:
            raise InvalidInputError(
                f"the artifact has {len(content)} bytes; the limit is {self.policy.max_bytes}"
                " (KE_EVIDENCE_MAX_BYTES)",
                details={"reason": "too_large", "limit": self.policy.max_bytes},
            )
        request_digest = req.request_digest()

        def run() -> IngestResult:
            with self.store.write_transaction() as conn:
                if req.idempotency_key is not None:
                    replay = self._replay(conn, req.idempotency_key, request_digest)
                    if replay is not None:
                        return replay
                result = self._ingest(conn, req)
                if req.idempotency_key is not None:
                    conn.execute(
                        "INSERT INTO ingest_requests (idempotency_key, source_id, record_id,"
                        " request_digest, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            req.idempotency_key,
                            req.source_id,
                            req.record_id,
                            request_digest,
                            result.model_dump_json(),
                            _now(),
                        ),
                    )
                return result

        result = self.store.guarded(run)
        if result.revision_created or result.head_changed or result.renamed_from:
            _logger.info(
                "evidence: %s/%s -> %s (new revision: %s, renamed from: %s)",
                req.source_id,
                req.record_id,
                result.revision.revision_id,
                result.revision_created,
                result.renamed_from,
            )
        return result

    def _replay(
        self, conn: sqlite3.Connection, key: str, request_digest: str
    ) -> IngestResult | None:
        row = conn.execute(
            "SELECT request_digest, result_json FROM ingest_requests WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise InvalidInputError(
                "the idempotency key was already used with different inputs",
                details={"reason": "idempotency_conflict", "idempotency_key": key},
            )
        stored = IngestResult.model_validate_json(row["result_json"])
        return stored.model_copy(
            update={
                "replayed": True,
                "revision_created": False,
                "record_created": False,
                "head_changed": False,
                "renamed_from": None,
                "restored": False,
            }
        )

    def _ingest(self, conn: sqlite3.Connection, req: IngestRequest) -> IngestResult:
        source = conn.execute(
            "SELECT workspace_id FROM sources WHERE source_id = ?", (req.source_id,)
        ).fetchone()
        if source is None:
            raise UnknownScopeError(
                f"unknown source {req.source_id!r}", details={"kind": "source", "id": req.source_id}
            )
        now = _now()
        row = conn.execute(
            "SELECT * FROM records WHERE source_id = ? AND record_id = ?",
            (req.source_id, req.record_id),
        ).fetchone()
        record_created = row is None
        renamed_from: str | None = None
        if row is None:
            self._check_path_free(conn, req.source_id, req.record_id, req.logical_path)
            rights = req.rights or RightsInfo(retention=self.policy.default_retention)
            conn.execute(
                "INSERT INTO records (source_id, record_id, logical_path, canonical_uri, title,"
                " retention, license, rights_notes, bibliographic_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req.source_id,
                    req.record_id,
                    req.logical_path,
                    req.canonical_uri,
                    req.title or "",
                    rights.retention.value,
                    rights.license,
                    rights.notes,
                    (req.bibliographic or Bibliographic()).model_dump_json(),
                    now,
                    now,
                ),
            )
        else:
            update = _RecordUpdate(
                req.source_id,
                req.record_id,
                logical_path=req.logical_path,
                canonical_uri=req.canonical_uri,
                title=req.title,
                bibliographic=req.bibliographic,
                rights=req.rights,
            )
            renamed_from = self._update_record(conn, row, update, now)
        record_row = conn.execute(
            "SELECT * FROM records WHERE source_id = ? AND record_id = ?",
            (req.source_id, req.record_id),
        ).fetchone()
        retention = RetentionMode(record_row["retention"])
        if retention == RetentionMode.TOMBSTONE:
            raise DeniedError(
                "the record's retention is 'tombstone': it accepts no new bytes",
                details={"source_id": req.source_id, "record_id": req.record_id},
            )

        content = req.content
        digest = sha256_digest(content) if content is not None else str(req.digest)
        length = len(content) if content is not None else int(req.byte_length or 0)
        retain = content is not None and retention == RetentionMode.RETAIN
        reason = req.unavailable_reason
        external = req.external_locator
        if content is not None and not retain:
            reason = "the record's retention policy is digest_only"
        if not retain:
            external = external or req.canonical_uri or record_row["canonical_uri"]
            if external is None:
                raise InvalidInputError(
                    "bytes are not retained: an external_locator or canonical_uri is required",
                    details={"reason": "external_locator_required"},
                )
        revision_id = derive_revision_id(req.source_id, req.record_id, digest)
        existing = conn.execute(
            "SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        revision_created = existing is None
        restored = False
        if existing is None:
            seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM revisions").fetchone()[0])
            observed = (req.observed_at or datetime.now(UTC)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO revisions (revision_id, seq, source_id, record_id, digest,"
                " byte_length, media_type, observed_at, upstream_version, upstream_date,"
                " bytes_retained, unavailable_reason, external_locator)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    seq,
                    req.source_id,
                    req.record_id,
                    digest,
                    length,
                    req.media_type,
                    observed,
                    req.upstream_version,
                    req.upstream_date,
                    int(retain),
                    None if retain else reason,
                    None if retain else external,
                ),
            )
            if retain and content is not None:
                conn.execute(
                    "INSERT INTO revision_bytes (revision_id, content) VALUES (?, ?)",
                    (revision_id, content),
                )
        elif existing["deleted_at"] is not None and retain and content is not None:
            # The same bytes arrive again after a deletion: retain them again.
            conn.execute(
                "UPDATE revisions SET deleted_at = NULL, bytes_retained = 1,"
                " unavailable_reason = NULL, external_locator = NULL WHERE revision_id = ?",
                (revision_id,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO revision_bytes (revision_id, content) VALUES (?, ?)",
                (revision_id, content),
            )
            conn.execute("DELETE FROM tombstones WHERE revision_id = ?", (revision_id,))
            restored = True

        head_changed = (
            record_row["current_revision_id"] != revision_id or record_row["deleted_at"] is not None
        )
        if head_changed:
            conn.execute(
                "UPDATE records SET current_revision_id = ?, deleted_at = NULL, updated_at = ?"
                " WHERE source_id = ? AND record_id = ?",
                (revision_id, now, req.source_id, req.record_id),
            )
        media_type = existing["media_type"] if existing is not None else req.media_type
        evidence_ids = self._record_spans(conn, req, revision_id, media_type, now)
        if req.derivation is not None:
            self._record_derivation(conn, req.derivation, revision_id, now)
        if revision_created or head_changed or restored:
            self._outbox(conn, "revision_added", req.source_id, req.record_id, revision_id, {})
        if renamed_from is not None:
            self._outbox(
                conn,
                "record_renamed",
                req.source_id,
                req.record_id,
                None,
                {"from": renamed_from, "to": req.logical_path},
            )
        info = self._revision(conn, revision_id)
        record = self._source_record(conn, req.source_id, req.record_id)
        return IngestResult(
            record=record,
            revision=info.revision,
            revision_created=revision_created,
            record_created=record_created,
            head_changed=head_changed,
            renamed_from=renamed_from,
            restored=restored,
            evidence_ids=evidence_ids,
        )

    def _check_path_free(
        self, conn: sqlite3.Connection, source_id: str, record_id: str, path: str | None
    ) -> None:
        if path is None:
            return
        other = conn.execute(
            "SELECT record_id FROM records WHERE source_id = ? AND logical_path = ?"
            " AND record_id != ?",
            (source_id, path, record_id),
        ).fetchone()
        if other is not None:
            raise InvalidInputError(
                f"logical path {path!r} already belongs to record {other['record_id']!r}",
                details={"reason": "path_conflict", "record_id": other["record_id"]},
            )

    def _update_record(
        self, conn: sqlite3.Connection, row: sqlite3.Row, req: _RecordUpdate, now: str
    ) -> str | None:
        updates: dict[str, Any] = {}
        renamed_from: str | None = None
        if req.logical_path is not None and req.logical_path != row["logical_path"]:
            self._check_path_free(conn, req.source_id, req.record_id, req.logical_path)
            renamed_from = row["logical_path"]
            updates["logical_path"] = req.logical_path
            if renamed_from is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO record_aliases (source_id, record_id, alias,"
                    " recorded_at) VALUES (?, ?, ?, ?)",
                    (req.source_id, req.record_id, renamed_from, now),
                )
            conn.execute(
                "DELETE FROM record_aliases WHERE source_id = ? AND record_id = ? AND alias = ?",
                (req.source_id, req.record_id, req.logical_path),
            )
        if req.canonical_uri is not None and req.canonical_uri != row["canonical_uri"]:
            updates["canonical_uri"] = req.canonical_uri
        if req.title is not None and req.title != row["title"]:
            updates["title"] = req.title
        if req.bibliographic is not None:
            updates["bibliographic_json"] = req.bibliographic.model_dump_json()
        if req.rights is not None:
            updates["retention"] = req.rights.retention.value
            updates["license"] = req.rights.license
            updates["rights_notes"] = req.rights.notes
        if updates:
            updates["updated_at"] = now
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE records SET {assignments} WHERE source_id = ? AND record_id = ?",
                (*updates.values(), req.source_id, req.record_id),
            )
        return renamed_from

    def _record_spans(
        self,
        conn: sqlite3.Connection,
        req: IngestRequest,
        revision_id: str,
        media_type: str,
        now: str,
    ) -> list[str]:
        content = req.content
        if content is None:
            return []
        extracted: list[ExtractedSpan] = []
        if req.spans is None:
            extracted = default_spans(content, media_type)
        else:
            for index, span in enumerate(req.spans):
                result = slice_locator(
                    content,
                    span.locator,
                    media_type=media_type,
                    upstream_version=req.upstream_version,
                )
                if result.excerpt is None:
                    raise InvalidInputError(
                        f"span {index} does not address the content: {result.reason}",
                        details={"reason": "span_invalid", "span": index},
                    )
                extracted.append(ExtractedSpan(span.locator, result.excerpt, span.extraction))
        return [self._insert_span(conn, revision_id, item, now) for item in extracted]

    def _insert_span(
        self, conn: sqlite3.Connection, revision_id: str, item: ExtractedSpan, now: str
    ) -> str:
        digest = item.excerpt_digest
        evidence_id = derive_evidence_id(revision_id, item.locator, digest)
        conn.execute(
            "INSERT OR IGNORE INTO evidence_spans (evidence_id, revision_id, locator_json,"
            " excerpt_digest, extraction_method, extraction_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                evidence_id,
                revision_id,
                canonical_json(item.locator.model_dump(mode="json")),
                digest,
                item.extraction.method,
                item.extraction.version,
                now,
            ),
        )
        return evidence_id

    def remember_span(self, revision_id: str, item: ExtractedSpan) -> str:
        """Persist an evidence span of an existing revision (e.g. a validated citation)."""

        def run() -> str:
            with self.store.write_transaction() as conn:
                self._revision(conn, revision_id)
                return self._insert_span(conn, revision_id, item, _now())

        return self.store.guarded(run)

    def _record_derivation(
        self, conn: sqlite3.Connection, data: DerivationInput, revision_id: str, now: str
    ) -> None:
        row = conn.execute("SELECT * FROM derivations WHERE run_id = ?", (data.run_id,)).fetchone()
        if row is None:
            for parent in data.parent_revision_ids:
                self._revision(conn, parent)
            conn.execute(
                "INSERT INTO derivations (run_id, module_id, module_version, parameters_digest,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (data.run_id, data.module_id, data.module_version, data.parameters_digest, now),
            )
            conn.executemany(
                "INSERT INTO derivation_revisions (run_id, role, revision_id, position)"
                " VALUES (?, 'parent', ?, ?)",
                [(data.run_id, rev, pos) for pos, rev in enumerate(data.parent_revision_ids)],
            )
        else:
            stored = self._derivation(conn, data.run_id)
            same = (
                stored.module_id == data.module_id
                and stored.module_version == data.module_version
                and stored.parameters_digest == data.parameters_digest
                and stored.parent_revision_ids == data.parent_revision_ids
            )
            if not same:
                raise InvalidInputError(
                    f"run {data.run_id!r} is already recorded with other lineage",
                    details={"reason": "derivation_conflict", "run_id": data.run_id},
                )
        position = int(
            conn.execute(
                "SELECT COUNT(*) FROM derivation_revisions WHERE run_id = ? AND role = 'output'",
                (data.run_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT OR IGNORE INTO derivation_revisions (run_id, role, revision_id, position)"
            " VALUES (?, 'output', ?, ?)",
            (data.run_id, revision_id, position),
        )

    def _outbox(
        self,
        conn: sqlite3.Connection,
        event: str,
        source_id: str,
        record_id: str,
        revision_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO evidence_outbox (event, source_id, record_id, revision_id, payload_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event, source_id, record_id, revision_id, canonical_json(payload), _now()),
        )

    # ── rename and deletion ──────────────────────────────────────

    def rename(self, source_id: str, record_id: str, new_path: str) -> SourceRecord:
        """Move a record to ``new_path``; the old path becomes an alias (identity unchanged)."""
        if new_path.startswith("/") or ".." in new_path.split("/") or not new_path:
            raise InvalidInputError("the new path must be a relative POSIX path")

        def run() -> SourceRecord:
            with self.store.write_transaction() as conn:
                row = self.store.record_row(conn, source_id, record_id)
                if row["logical_path"] == new_path:
                    return self._source_record(conn, source_id, record_id)
                old = self._update_record(
                    conn, row, _RecordUpdate(source_id, record_id, logical_path=new_path), _now()
                )
                self._outbox(
                    conn,
                    "record_renamed",
                    source_id,
                    record_id,
                    None,
                    {"from": old, "to": new_path},
                )
                return self._source_record(conn, source_id, record_id)

        record = self.store.guarded(run)
        _logger.info("evidence: renamed %s/%s to %s", source_id, record_id, new_path)
        return record

    def delete_record(self, source_id: str, record_id: str, *, reason: str) -> dict[str, Any]:
        """Delete a record's bytes per the policy: ``tombstone`` or ``purge`` (ADR 0008)."""
        reason = reason.strip()
        if not reason or len(reason) > 500:
            raise InvalidInputError("a deletion reason (1-500 characters) is required")
        mode = self.policy.on_delete

        def run() -> dict[str, Any]:
            with self.store.write_transaction() as conn:
                self.store.record_row(conn, source_id, record_id)
                key = (source_id, record_id)
                revisions = conn.execute(
                    "SELECT revision_id, digest FROM revisions WHERE source_id = ? AND record_id = ?",
                    key,
                ).fetchall()
                now = _now()
                conn.execute(
                    "DELETE FROM ingest_requests WHERE source_id = ? AND record_id = ?", key
                )
                for rev in revisions:
                    conn.execute(
                        "DELETE FROM revision_bytes WHERE revision_id = ?", (rev["revision_id"],)
                    )
                if mode == "purge":
                    conn.executemany(
                        "DELETE FROM derivation_revisions WHERE revision_id = ?",
                        [(rev["revision_id"],) for rev in revisions],
                    )
                    conn.execute(
                        "DELETE FROM tombstones WHERE source_id = ? AND record_id = ?", key
                    )
                    conn.execute("DELETE FROM records WHERE source_id = ? AND record_id = ?", key)
                else:
                    for rev in revisions:
                        conn.execute(
                            "INSERT OR REPLACE INTO tombstones (source_id, record_id, revision_id,"
                            " digest, deleted_at, reason) VALUES (?, ?, ?, ?, ?, ?)",
                            (source_id, record_id, rev["revision_id"], rev["digest"], now, reason),
                        )
                    conn.execute(
                        "UPDATE revisions SET bytes_retained = 0, deleted_at = ?,"
                        " unavailable_reason = ?, external_locator = NULL"
                        " WHERE source_id = ? AND record_id = ?",
                        (now, f"deleted: {reason}"[:500], *key),
                    )
                    conn.execute(
                        "UPDATE records SET deleted_at = ?, updated_at = ?"
                        " WHERE source_id = ? AND record_id = ?",
                        (now, now, *key),
                    )
                self._outbox(conn, "record_deleted", source_id, record_id, None, {"mode": mode})
                return {
                    "source_id": source_id,
                    "record_id": record_id,
                    "mode": mode,
                    "revisions": len(revisions),
                    "tombstones": len(revisions) if mode == "tombstone" else 0,
                    "deleted_at": now,
                }

        result = self.store.guarded(run)
        _logger.info("evidence: deleted %s/%s (%s)", source_id, record_id, mode)
        return result

    # ── reads ────────────────────────────────────────────────────

    def _source_record(
        self, conn: sqlite3.Connection, source_id: str, record_id: str
    ) -> SourceRecord:
        row = self.store.record_row(conn, source_id, record_id)
        return self.store.records_with_labels(conn, [row])[0]

    def record_state(self, source_id: str, record_id: str) -> RecordState:
        with self.snapshot() as conn:
            row = self.store.record_row(conn, source_id, record_id)
            record = self.store.records_with_labels(conn, [row])[0]
            return RecordState(record, row["current_revision_id"], row["deleted_at"])

    def _revision(self, conn: sqlite3.Connection, revision_id: str) -> RevisionInfo:
        row = conn.execute(
            "SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown revision {revision_id!r}", details={"kind": "revision", "id": revision_id}
            )
        return _revision_info(row)

    def get_revision(self, revision_id: str) -> RevisionInfo | None:
        with self.snapshot() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        return _revision_info(row) if row is not None else None

    def list_revisions(self, source_id: str, record_id: str) -> list[RevisionInfo]:
        """Revisions of a record, oldest first."""
        with self.snapshot() as conn:
            self.store.record_row(conn, source_id, record_id)
            rows = conn.execute(
                "SELECT * FROM revisions WHERE source_id = ? AND record_id = ? ORDER BY seq",
                (source_id, record_id),
            ).fetchall()
        return [_revision_info(row) for row in rows]

    def revision_bytes(self, revision_id: str) -> bytes | None:
        with self.snapshot() as conn:
            row = conn.execute(
                "SELECT content FROM revision_bytes WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        return bytes(row[0]) if row is not None else None

    def tombstone(self, revision_id: str) -> Tombstone | None:
        with self.snapshot() as conn:
            row = conn.execute(
                "SELECT * FROM tombstones WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        if row is None:
            return None
        return Tombstone(
            row["source_id"],
            row["record_id"],
            row["revision_id"],
            row["digest"],
            row["deleted_at"],
            row["reason"],
        )

    def get_span(self, evidence_id: str) -> SpanInfo | None:
        with self.snapshot() as conn:
            row = conn.execute(
                "SELECT e.*, r.source_id, r.record_id FROM evidence_spans e"
                " JOIN revisions r USING (revision_id) WHERE e.evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        span = EvidenceSpan(
            evidence_id=row["evidence_id"],
            revision_id=row["revision_id"],
            locator=_LOCATOR.validate_json(row["locator_json"]),
            excerpt_digest=row["excerpt_digest"],
            extraction=ExtractionInfo(
                method=row["extraction_method"], version=row["extraction_version"]
            ),
        )
        return SpanInfo(span, row["source_id"], row["record_id"])

    def list_spans(self, revision_id: str) -> list[EvidenceSpan]:
        with self.snapshot() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT evidence_id FROM evidence_spans WHERE revision_id = ?"
                    " ORDER BY evidence_id",
                    (revision_id,),
                )
            ]
        out: list[EvidenceSpan] = []
        for evidence_id in ids:
            info = self.get_span(evidence_id)
            if info is not None:
                out.append(info.span)
        return out

    def find_span(self, revision_id: str, locator: BaseModel) -> str | None:
        """The stored evidence id of ``locator`` in ``revision_id``, if one was recorded."""
        with self.snapshot() as conn:
            row = conn.execute(
                "SELECT evidence_id FROM evidence_spans WHERE revision_id = ? AND locator_json = ?"
                " ORDER BY evidence_id LIMIT 1",
                (revision_id, canonical_json(locator.model_dump(mode="json"))),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def find_records(self, source_id: str, path: str) -> list[str]:
        """Record ids whose current path or an alias is ``path`` (several: ambiguous)."""
        with self.snapshot() as conn:
            rows = conn.execute(
                "SELECT record_id FROM records WHERE source_id = ? AND logical_path = ?"
                " UNION SELECT record_id FROM record_aliases WHERE source_id = ? AND alias = ?",
                (source_id, path, source_id, path),
            ).fetchall()
        return sorted({row[0] for row in rows})

    def _derivation(self, conn: sqlite3.Connection, run_id: str) -> Derivation:
        row = conn.execute("SELECT * FROM derivations WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownScopeError(
                f"unknown run {run_id!r}", details={"kind": "run", "id": run_id}
            )
        links = conn.execute(
            "SELECT role, revision_id FROM derivation_revisions WHERE run_id = ?"
            " ORDER BY role, position",
            (run_id,),
        ).fetchall()
        return Derivation(
            run_id=row["run_id"],
            module_id=row["module_id"],
            module_version=row["module_version"],
            parameters_digest=row["parameters_digest"],
            parent_revision_ids=tuple(r[1] for r in links if r[0] == "parent"),
            output_revision_ids=tuple(r[1] for r in links if r[0] == "output"),
        )

    def derivations_producing(self, revision_id: str) -> list[Derivation]:
        """The runs that produced ``revision_id`` (provenance)."""
        with self.snapshot() as conn:
            runs = [
                r[0]
                for r in conn.execute(
                    "SELECT run_id FROM derivation_revisions WHERE revision_id = ?"
                    " AND role = 'output' ORDER BY run_id",
                    (revision_id,),
                )
            ]
            return [self._derivation(conn, run_id) for run_id in runs]

    # ── outbox ───────────────────────────────────────────────────

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """Unprocessed outbox events, oldest first."""
        if not 1 <= limit <= MAX_OUTBOX_PAGE:
            raise InvalidInputError(f"limit must be between 1 and {MAX_OUTBOX_PAGE}")
        with self.snapshot() as conn:
            rows = conn.execute(
                "SELECT seq, event, source_id, record_id, revision_id, payload_json, created_at"
                " FROM evidence_outbox WHERE processed_at IS NULL ORDER BY seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "event": row["event"],
                "source_id": row["source_id"],
                "record_id": row["record_id"],
                "revision_id": row["revision_id"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def ack_outbox(self, seqs: Sequence[int]) -> int:
        """Mark outbox events processed (idempotent); return how many changed."""
        values = sorted({int(seq) for seq in seqs})
        if len(values) > MAX_OUTBOX_PAGE:
            raise InvalidInputError(f"at most {MAX_OUTBOX_PAGE} events per call")

        def run() -> int:
            with self.store.write_transaction() as conn:
                now = _now()
                changed = 0
                for seq in values:
                    changed += conn.execute(
                        "UPDATE evidence_outbox SET processed_at = ? WHERE seq = ?"
                        " AND processed_at IS NULL",
                        (now, seq),
                    ).rowcount
                return changed

        return self.store.guarded(run)

    def stats(self) -> dict[str, int]:
        """Counts of the evidence tables."""
        with self.snapshot() as conn:
            counts = {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for name, table in (
                    ("revisions", "revisions"),
                    ("retained_revisions", "revision_bytes"),
                    ("evidence_spans", "evidence_spans"),
                    ("aliases", "record_aliases"),
                    ("derivations", "derivations"),
                    ("tombstones", "tombstones"),
                )
            }
            counts["outbox_pending"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM evidence_outbox WHERE processed_at IS NULL"
                ).fetchone()[0]
            )
        return counts


def _revision_info(row: sqlite3.Row) -> RevisionInfo:
    revision = Revision(
        revision_id=row["revision_id"],
        source_id=row["source_id"],
        record_id=row["record_id"],
        digest=row["digest"],
        byte_length=int(row["byte_length"]),
        media_type=row["media_type"],
        observed_at=_ts(row["observed_at"]),
        upstream_version=row["upstream_version"],
        upstream_date=row["upstream_date"],
        bytes_retained=bool(row["bytes_retained"]),
        unavailable_reason=row["unavailable_reason"],
    )
    return RevisionInfo(revision, row["external_locator"], row["deleted_at"], int(row["seq"]))


def _parse[M: BaseModel](model: type[M], value: M | dict[str, Any], what: str) -> M:
    if isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise InvalidInputError(
            f"invalid {what}", details={"errors": validation_messages(exc)}
        ) from exc


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "MAX_OUTBOX_PAGE",
    "DerivationInput",
    "EvidenceStore",
    "IngestRequest",
    "IngestResult",
    "RecordState",
    "RevisionInfo",
    "SpanInfo",
    "SpanInput",
    "Tombstone",
]
