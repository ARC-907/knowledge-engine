"""Contract fixtures of ``knowledge_engine.knowledge.contracts`` and the JSON Schema snapshot.

The JSON Schema of every public contract model is pinned in
``tests/snapshots/knowledge_contracts.json``. To accept an intended contract
change, regenerate and review the diff::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_knowledge_contracts.py

(run from ``engine/``). A contract change must also bump or extend
``schema_version`` as described in ADR 0007.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from knowledge_engine.knowledge import errors
from knowledge_engine.knowledge.adapter import (
    AdapterCapabilities,
    AdapterSearchResult,
    SourceAdapter,
)
from knowledge_engine.knowledge.contracts import (
    CONTRACT_VERSION,
    Citation,
    CodeLocator,
    CurationStatus,
    EffectiveScope,
    ErrorInfo,
    EvidenceSpan,
    ExtractionInfo,
    Locator,
    OpenResult,
    QueryContext,
    RankingInfo,
    RecordScope,
    ResearchType,
    Revision,
    SearchEnvelope,
    SearchHit,
    SourceRecord,
    SourceState,
    SourceStatus,
    TextLocator,
    TranscriptLocator,
    VerificationState,
    contract_json_schema,
    derive_evidence_id,
    derive_revision_id,
    normalize_tag,
    parse_query_context,
    sha256_digest,
)
from knowledge_engine.knowledge.errors import InvalidInputError, KnowledgeError

SNAPSHOT = Path(__file__).parent / "snapshots" / "knowledge_contracts.json"
LOCATOR = TypeAdapter(Locator)
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_contract_schema_matches_snapshot() -> None:
    rendered = _render(contract_json_schema())
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(rendered, encoding="utf-8")
    assert SNAPSHOT.is_file(), "missing snapshot; run with UPDATE_SNAPSHOTS=1 (see docstring)"
    assert rendered == SNAPSHOT.read_text(encoding="utf-8"), (
        "knowledge contract schema changed; if intended, regenerate with UPDATE_SNAPSHOTS=1 "
        "(see tests/test_knowledge_contracts.py)"
    )


def test_every_public_record_carries_schema_version() -> None:
    models = contract_json_schema()["models"]
    for name in (
        "QueryContext",
        "SearchHit",
        "SearchEnvelope",
        "SourceStatus",
        "OpenResult",
        "SourceRecord",
        "Revision",
        "EvidenceSpan",
        "Derivation",
        "Citation",
        "Workspace",
        "Project",
        "Source",
        "Collection",
        "AdapterCapabilities",
    ):
        props = models[name]["properties"]
        assert props["schema_version"]["const"] == CONTRACT_VERSION, name


def test_other_schema_versions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryContext.model_validate({"schema_version": 2, "workspace_id": "default"})


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryContext.model_validate({"workspace_id": "default", "scope": "everything"})


# ── Vocabularies ─────────────────────────────────────────────────────


def test_controlled_vocabularies() -> None:
    assert [t.value for t in ResearchType] == [
        "reference",
        "paper",
        "article",
        "transcript",
        "code",
        "note",
        "dataset",
        "synthesis",
        "other",
    ]
    assert [s.value for s in CurationStatus] == ["unclassified", "inbox", "reviewed", "archived"]
    assert [v.value for v in VerificationState] == [
        "verified",
        "stale",
        "unavailable",
        "ambiguous",
        "unsupported",
    ]


def test_unknown_research_type_fails_validation() -> None:
    with pytest.raises(ValidationError):
        QueryContext(workspace_id="default", research_types=["podcast"])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Machine Learning", "machine-learning"),
        ("  RAG  ", "rag"),
        ("a/b:c", "a/b:c"),
        ("\uff37\uff29\uff24\uff25", "wide"),  # NFKC folds full-width letters
    ],
)
def test_tags_are_normalized(raw: str, expected: str) -> None:
    assert normalize_tag(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "-lead", "bang!", "école", "x" * 65])
def test_invalid_tags_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid tag"):
        normalize_tag(raw)


def test_query_context_dedupes_and_normalizes() -> None:
    ctx = QueryContext(
        workspace_id="default",
        source_ids=["corpus:base", "corpus:base"],
        tags=["B", "a", "b"],
    )
    assert ctx.source_ids == ("corpus:base",)
    assert ctx.tags == ("a", "b")
    # Omitted and explicitly empty selections stay distinguishable.
    assert QueryContext(workspace_id="default").source_ids is None
    assert QueryContext(workspace_id="default", source_ids=[]).source_ids == ()


@pytest.mark.parametrize(
    "bad",
    [
        {"workspace_id": "Default"},
        {"workspace_id": "default", "source_ids": ["corpus"]},
        {"workspace_id": "default", "limit": 0},
        {"workspace_id": "default", "limit": 101},
        {"workspace_id": "default", "mode": "semantic"},
        {"workspace_id": "default", "source_ids": [f"s:{i}" for i in range(201)]},
    ],
)
def test_parse_query_context_rejects_invalid_input(bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidInputError) as exc:
        parse_query_context(bad)
    assert exc.value.code == "invalid_input"
    assert exc.value.details["errors"]


def test_caller_cannot_supply_a_principal() -> None:
    payload = {
        "workspace_id": "default",
        "principal": {"principal_id": "me", "allowed_workspaces": ["default"]},
    }
    with pytest.raises(InvalidInputError, match="principal"):
        parse_query_context(payload)
    with pytest.raises(InvalidInputError):
        parse_query_context(["not", "an", "object"])


# ── Locators ─────────────────────────────────────────────────────────


def test_text_locator_is_half_open_utf8_bytes() -> None:
    text = "Überblick\n".encode()
    loc = TextLocator(heading="Intro", start_byte=0, end_byte=len("Über".encode()))
    assert text[loc.start_byte : loc.end_byte].decode() == "Über"
    assert TextLocator(start_byte=3, end_byte=3).end_byte == 3  # empty span is allowed
    with pytest.raises(ValidationError, match="half-open"):
        TextLocator(start_byte=5, end_byte=4)


def test_transcript_and_pdf_locators() -> None:
    seg = TranscriptLocator(start_ms=1_000, end_ms=2_500, speaker="Host")
    assert seg.end_ms - seg.start_ms == 1_500
    with pytest.raises(ValidationError):
        TranscriptLocator(start_ms=10, end_ms=5)
    pdf = LOCATOR.validate_python(
        {"kind": "pdf", "page": 3, "bbox": {"x0": 0, "y0": 0, "x1": 10, "y1": 20}}
    )
    assert pdf.kind == "pdf"
    with pytest.raises(ValidationError):
        LOCATOR.validate_python({"kind": "pdf", "page": 0})
    with pytest.raises(ValidationError):
        LOCATOR.validate_python(
            {"kind": "pdf", "page": 1, "bbox": {"x0": 5, "y0": 0, "x1": 1, "y1": 1}}
        )


def test_code_locator_requires_commit_xor_snapshot() -> None:
    clean = CodeLocator(
        repository_id="repo-a",
        commit=COMMIT,
        path="src/m.py",
        symbol="m.f",
        start_line=3,
        end_line=4,
    )
    assert clean.end_line == 4  # exactly line 3
    dirty = CodeLocator(repository_id="repo-a", snapshot_digest=sha256_digest(b"tree"), path="m.py")
    assert dirty.commit is None
    for bad in (
        {"repository_id": "r", "path": "m.py"},
        {
            "repository_id": "r",
            "path": "m.py",
            "commit": COMMIT,
            "snapshot_digest": sha256_digest(b"x"),
        },
        {"repository_id": "r", "path": "/abs.py", "commit": COMMIT},
        {"repository_id": "r", "path": "../up.py", "commit": COMMIT},
        {"repository_id": "r", "path": "m.py", "commit": COMMIT, "start_line": 3, "end_line": 3},
        {"repository_id": "r", "path": "m.py", "commit": COMMIT, "start_line": 3},
    ):
        with pytest.raises(ValidationError):
            CodeLocator.model_validate(bad)


def test_unsupported_locator_is_explicit() -> None:
    loc = LOCATOR.validate_python({"kind": "unsupported", "reason": "no page positions"})
    assert loc.kind == "unsupported"
    with pytest.raises(ValidationError):
        LOCATOR.validate_python({"kind": "audio", "start_ms": 1})


# ── Identity ─────────────────────────────────────────────────────────


def test_revision_identity_is_content_addressed_per_record() -> None:
    digest = sha256_digest(b"same bytes")
    a = derive_revision_id("corpus:base", "file-a", digest)
    assert a == derive_revision_id("corpus:base", "file-a", digest)  # reindex-stable
    # Two records sharing bytes keep distinct revisions.
    assert a != derive_revision_id("corpus:base", "file-b", digest)
    assert a != derive_revision_id("corpus:other", "file-a", digest)
    revision = Revision(
        revision_id=a,
        source_id="corpus:base",
        record_id="file-a",
        digest=digest,
        byte_length=10,
        media_type="text/markdown",
        observed_at=datetime(2026, 10, 30, tzinfo=UTC),
    )
    assert revision.bytes_retained
    with pytest.raises(ValidationError, match="derive_revision_id"):
        Revision.model_validate({**revision.model_dump(), "revision_id": "rev_" + "0" * 32})
    with pytest.raises(ValidationError, match="records why"):
        Revision.model_validate({**revision.model_dump(), "bytes_retained": False})


def test_evidence_identity_and_citation() -> None:
    revision_id = derive_revision_id("corpus:base", "file-a", sha256_digest(b"body"))
    locator = TextLocator(start_byte=0, end_byte=4)
    excerpt = sha256_digest(b"body")
    evidence_id = derive_evidence_id(revision_id, locator, excerpt)
    span = EvidenceSpan(
        evidence_id=evidence_id,
        revision_id=revision_id,
        locator=locator,
        excerpt_digest=excerpt,
        extraction=ExtractionInfo(method="markdown", version="1"),
    )
    assert span.evidence_id.startswith("ev_")
    assert derive_evidence_id(revision_id, TextLocator(start_byte=0, end_byte=5), excerpt) != (
        evidence_id
    )
    citation = Citation(
        evidence_id=evidence_id,
        source_id="corpus:base",
        record_id="file-a",
        revision_id=revision_id,
        locator=locator,
        verification=VerificationState.VERIFIED,
    )
    # Missing bibliographic metadata stays absent, never invented.
    assert citation.export.title is None and citation.export.authors == ()
    with pytest.raises(ValidationError):
        EvidenceSpan.model_validate({**span.model_dump(), "evidence_id": "ev_" + "1" * 32})


def test_source_record_identity_and_unclassified_defaults() -> None:
    record = SourceRecord(
        source_id="corpus:base",
        record_id="file-0123",
        scope=RecordScope(workspace_id="default"),
        logical_path="libs/a/readme.md",
        collection_ids=["b", "a", "a"],
    )
    assert record.research_type is ResearchType.OTHER
    assert record.curation_status is CurationStatus.UNCLASSIFIED
    assert record.scope.project_id is None
    assert record.collection_ids == ("a", "b")
    with pytest.raises(ValidationError):
        SourceRecord(source_id="corpus:base", record_id="x", scope=record.scope)
    with pytest.raises(ValidationError):
        SourceRecord(
            source_id="corpus:base", record_id="x", scope=record.scope, logical_path="../etc"
        )
    with pytest.raises(ValidationError):
        SourceRecord(source_id="corpus:base", record_id="a/b", scope=record.scope, logical_path="a")


# ── Envelopes and source status ──────────────────────────────────────


def _scope() -> EffectiveScope:
    return EffectiveScope(
        workspace_id="default", profile_id="default", source_ids=("corpus:base", "serena:proj")
    )


def _hit(source: str = "corpus:base") -> SearchHit:
    return SearchHit(
        source_id=source,
        record_id="file-1",
        scope=RecordScope(workspace_id="default"),
        ranking=RankingInfo(native_rank=1, native_score=-3.2, score_kind="bm25"),
    )


def _failed(source: str) -> SourceStatus:
    return SourceStatus(
        source_id=source,
        state=SourceState.UNAVAILABLE,
        error=ErrorInfo(**errors.UnavailableError("serena is not running").to_dict()),
    )


def test_unavailable_optional_source_gives_partial_envelope() -> None:
    envelope = SearchEnvelope(
        hits=(_hit(),),
        effective_scope=_scope(),
        source_status=(
            SourceStatus(source_id="corpus:base", state=SourceState.OK, hit_count=1),
            _failed("serena:proj"),
        ),
        partial=True,
    )
    assert envelope.partial
    assert envelope.source_status[1].error is not None
    assert envelope.source_status[1].error.code == "unavailable"
    with pytest.raises(ValidationError, match="partial"):
        SearchEnvelope.model_validate({**envelope.model_dump(), "partial": False})


def test_all_sources_failing_is_not_an_empty_success() -> None:
    with pytest.raises(ValidationError, match="UnavailableError"):
        SearchEnvelope(
            effective_scope=_scope(),
            source_status=(_failed("corpus:base"), _failed("serena:proj")),
            partial=True,
        )


def test_no_matches_with_healthy_sources_is_a_valid_empty_envelope() -> None:
    envelope = SearchEnvelope(
        effective_scope=_scope(),
        source_status=(SourceStatus(source_id="corpus:base", state=SourceState.OK),),
    )
    assert envelope.hits == () and not envelope.partial


def test_source_status_rules() -> None:
    with pytest.raises(ValidationError, match="must carry an error"):
        SourceStatus(source_id="corpus:base", state=SourceState.TIMEOUT)
    with pytest.raises(ValidationError):
        SearchEnvelope(
            hits=(_hit("other:src"),),
            effective_scope=_scope(),
            source_status=(SourceStatus(source_id="corpus:base", state=SourceState.OK),),
        )


def test_open_result_verification_rules() -> None:
    revision_id = derive_revision_id("corpus:base", "file-1", sha256_digest(b"x"))
    base: dict[str, Any] = {
        "source_id": "corpus:base",
        "record_id": "file-1",
        "revision_id": revision_id,
        "scope": {"workspace_id": "default"},
        "media_type": "text/markdown",
        "source_status": {"source_id": "corpus:base", "state": "ok"},
    }
    ok = OpenResult.model_validate(
        {**base, "verification": "verified", "content": "x", "digest": sha256_digest(b"x")}
    )
    assert ok.verification is VerificationState.VERIFIED
    with pytest.raises(ValidationError):
        OpenResult.model_validate({**base, "verification": "verified"})
    with pytest.raises(ValidationError):
        OpenResult.model_validate({**base, "verification": "unavailable", "content": "x"})
    gone = OpenResult.model_validate({**base, "verification": "unavailable"})
    assert gone.content is None


# ── Errors and adapters ──────────────────────────────────────────────


def test_error_codes_are_stable_and_serializable() -> None:
    assert errors.ERROR_CODES == (
        "invalid_scope",
        "unknown_scope",
        "invalid_input",
        "denied",
        "unavailable",
        "incompatible",
        "timeout",
        "cancelled",
        "output_invalid",
        "internal_error",
    )
    assert set(errors.ERRORS_BY_CODE) == set(errors.ERROR_CODES)
    assert set(errors.CLI_EXIT_CODES) == set(errors.ERROR_CODES)
    for code, cls in errors.ERRORS_BY_CODE.items():
        exc = cls("boom", details={"k": 1})
        assert isinstance(exc, KnowledgeError)
        payload = exc.to_dict()
        assert payload == {
            "code": code,
            "message": "boom",
            "details": {"k": 1},
            "retryable": cls.retryable,
        }
        json.dumps(payload)
        again = errors.from_dict(payload)
        assert type(again) is cls and again.details == {"k": 1}
        assert 400 <= cls.http_status < 600
    assert type(errors.from_dict({"code": "nope"})) is errors.InternalError


class _FakeAdapter:
    source_id = "fake:one"

    def describe_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(source_id=self.source_id, source_kind="other", modes=("fts",))

    def health(self) -> SourceStatus:
        return SourceStatus(source_id=self.source_id, state=SourceState.OK)

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        return AdapterSearchResult(source_id=self.source_id, status=self.health())

    def open(
        self,
        context: QueryContext,
        scope: EffectiveScope,
        record_id: str,
        revision_id: str,
        locator: Any = None,
    ) -> OpenResult:
        raise errors.UnavailableError("nothing retained")


def test_adapter_protocol_is_structural() -> None:
    adapter = _FakeAdapter()
    assert isinstance(adapter, SourceAdapter)
    caps = adapter.describe_capabilities()
    assert caps.read_only and caps.contract_version == CONTRACT_VERSION
    result = adapter.search(QueryContext(workspace_id="default"), _scope(), "q")
    assert result.status.state is SourceState.OK
    assert not isinstance(object(), SourceAdapter)
