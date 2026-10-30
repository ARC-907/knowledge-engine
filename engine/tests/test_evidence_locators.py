"""Locator extraction and slicing fixtures: Markdown, plain text, transcript, code, PDF.

The fixtures under ``tests/fixtures/evidence`` pin the conventions of ADR 0007:
half-open ranges, UTF-8 byte offsets, integer milliseconds and 1-based code
lines with an exclusive end.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from knowledge_engine.knowledge.contracts import (
    CodeLocator,
    PdfLocator,
    QueryContext,
    TextLocator,
    TranscriptLocator,
    UnsupportedLocator,
    VerificationState,
    sha256_digest,
)
from knowledge_engine.knowledge.errors import InvalidInputError
from knowledge_engine.knowledge.evidence import extract
from knowledge_engine.knowledge.evidence.resolve import open_evidence, open_span
from knowledge_engine.knowledge.evidence.store import EvidenceStore
from knowledge_engine.knowledge.store import KnowledgeStore

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
COMMIT = "a" * 40


def test_markdown_sections_fixture() -> None:
    data = (FIXTURES / "sections.md").read_bytes()
    spans = extract.markdown_sections(data)
    assert [(s.locator.heading, s.locator.start_byte, s.locator.end_byte) for s in spans] == [
        ("Café notes", 30, 101),
        ("Usage", 101, 137),
    ]
    assert spans[0].excerpt.decode().startswith("# Café notes\n\nÜber alles.")
    assert b"# not a heading inside a fence" in spans[0].excerpt  # fences are not headings
    assert spans[1].excerpt == b"## Usage ##\n\nRun it.\n#not-a-heading\n"
    assert spans[1].excerpt_digest == sha256_digest(spans[1].excerpt)
    assert spans[0].extraction.method == "markdown-heading"


def test_text_spans_respect_utf8_boundaries() -> None:
    data = "Über".encode()
    assert extract.text_span(data, 0, 2).excerpt == "Ü".encode()
    with pytest.raises(InvalidInputError):
        extract.text_span(data, 0, 1)  # inside the two-byte "Ü"
    result = extract.slice_text(data, TextLocator(start_byte=0, end_byte=99))
    assert result.status == "unsupported" and result.reason and "bytes" in result.reason
    found = extract.find_text(b"a b a", "a")
    assert [(s.locator.start_byte, s.locator.end_byte) for s in found] == [(0, 1), (4, 5)]
    with pytest.raises(InvalidInputError):
        extract.find_text(b"x", "")
    whole = extract.whole_document(data)
    assert whole.locator == TextLocator(start_byte=0, end_byte=5)


def test_transcript_segments_fixture() -> None:
    data = (FIXTURES / "transcript.json").read_bytes()
    spans = extract.transcript_segments(data)
    assert [s.locator for s in spans] == [
        TranscriptLocator(start_ms=0, end_ms=1500, speaker="Host"),
        TranscriptLocator(start_ms=1500, end_ms=4200, speaker="Guest"),
        TranscriptLocator(start_ms=4200, end_ms=6000, speaker="Guest"),
    ]
    two = extract.slice_transcript(
        data, TranscriptLocator(start_ms=1500, end_ms=6000, speaker="Guest")
    )
    assert two.excerpt == b"Thanks for having me.\nLet us begin."
    misaligned = extract.slice_transcript(data, TranscriptLocator(start_ms=1000, end_ms=4200))
    assert misaligned.status == "unsupported" and "align" in str(misaligned.reason)
    speaker = extract.slice_transcript(
        data, TranscriptLocator(start_ms=0, end_ms=1500, speaker="Guest")
    )
    assert speaker.status == "unsupported"
    empty = extract.slice_transcript(data, TranscriptLocator(start_ms=7000, end_ms=8000))
    assert empty.status == "unsupported"


@pytest.mark.parametrize(
    "payload",
    [b"not json", b'{"segments": 3}', b'[{"start_ms": 5, "end_ms": 1, "text": "x"}]', b"[1]"],
)
def test_invalid_transcripts_are_rejected(payload: bytes) -> None:
    with pytest.raises(InvalidInputError):
        extract.parse_transcript(payload)


def test_python_symbols_fixture() -> None:
    data = (FIXTURES / "module_sample.py").read_bytes()
    spans = extract.python_symbols(data, repository_id="repo-a", path="pkg/mod.py", commit=COMMIT)
    got = [(s.locator.symbol, s.locator.start_line, s.locator.end_line) for s in spans]
    assert got == [
        ("helper", 4, 6),
        ("Widget", 8, 15),
        ("Widget.build", 9, 12),
        ("Widget.render", 13, 15),
        ("render", 17, 19),
    ]
    assert spans[0].excerpt == b"def helper(value):\n    return value * 2\n"
    with pytest.raises(InvalidInputError):
        extract.python_symbols(b"def (", repository_id="r", path="x.py", commit=COMMIT)


def test_code_slicing_checks_version_and_ambiguity() -> None:
    data = (FIXTURES / "module_sample.py").read_bytes()
    loc = CodeLocator(repository_id="r", commit=COMMIT, path="m.py", symbol="helper")
    media = extract.MEDIA_PYTHON
    ok = extract.slice_code(data, loc, media_type=media, upstream_version=COMMIT)
    assert ok.excerpt == b"def helper(value):\n    return value * 2\n"
    other = extract.slice_code(data, loc, media_type=media, upstream_version="b" * 40)
    assert other.status == "unsupported" and "commit" in str(other.reason)
    dup = data + b"\n\ndef helper():\n    pass\n"
    amb = extract.slice_code(dup, loc, media_type=media, upstream_version=COMMIT)
    assert amb.status == "ambiguous"
    lines = CodeLocator(repository_id="r", commit=COMMIT, path="m.py", start_line=17, end_line=19)
    assert extract.slice_code(data, lines, media_type=media, upstream_version=COMMIT).excerpt == (
        b'def render():\n    return "module-level"\n'
    )
    beyond = CodeLocator(repository_id="r", commit=COMMIT, path="m.py", start_line=1, end_line=99)
    assert extract.slice_code(data, beyond, media_type=media, upstream_version=COMMIT).status == (
        "unsupported"
    )


def test_dirty_snapshot_digest_is_deterministic() -> None:
    one = extract.snapshot_digest({"b.py": b"2", "a.py": b"1"})
    two = extract.snapshot_digest({"a.py": b"1", "b.py": b"2"})
    assert one == two and one.startswith("sha256:")
    assert extract.snapshot_digest({"a.py": b"1", "b.py": b"3"}) != one


def test_pdf_is_unsupported_not_invented() -> None:
    loc = extract.pdf_pages(b"%PDF-1.7 ...")
    assert isinstance(loc, UnsupportedLocator) and "PDF" in loc.reason
    result = extract.slice_locator(b"%PDF", PdfLocator(page=1), media_type=extract.MEDIA_PDF)
    assert result.status == "unsupported"
    assert extract.default_spans(b"%PDF", extract.MEDIA_PDF) == []


def test_locator_media_mismatch_is_unsupported() -> None:
    text = TextLocator(start_byte=0, end_byte=1)
    assert extract.slice_locator(b"x", text, media_type="application/pdf").status == "unsupported"
    seg = TranscriptLocator(start_ms=0, end_ms=1)
    assert extract.slice_locator(b"x", seg, media_type="text/plain").status == "unsupported"


# ── Resolution of each locator kind through the store ────────────────


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[EvidenceStore]:
    store = KnowledgeStore.open(tmp_path / "lab.db")
    store.create_workspace("Lab", workspace_id="lab")
    store.register_source("lab", "module:media", kind="module", display_name="Media")
    try:
        yield EvidenceStore(store)
    finally:
        store.close()


CTX = QueryContext(workspace_id="lab")


def test_transcript_and_code_resolve_through_open(lab: EvidenceStore) -> None:
    transcript = lab.ingest(
        {
            "source_id": "module:media",
            "record_id": "episode-1",
            "canonical_uri": "https://example.org/episode-1",
            "media_type": extract.MEDIA_TRANSCRIPT,
            "content": (FIXTURES / "transcript.json").read_bytes(),
        }
    )
    assert len(transcript.evidence_ids) == 3  # default extraction: one span per segment
    res = open_span(lab, CTX, transcript.evidence_ids[1])
    assert res.verification == VerificationState.VERIFIED
    assert res.result.content == "Thanks for having me."

    code = lab.ingest(
        {
            "source_id": "module:media",
            "record_id": "repo-a.mod",
            "logical_path": "pkg/mod.py",
            "media_type": extract.MEDIA_PYTHON,
            "content": (FIXTURES / "module_sample.py").read_bytes(),
            "upstream_version": COMMIT,
            "spans": [
                {
                    "locator": span.locator.model_dump(),
                    "extraction": span.extraction.model_dump(),
                }
                for span in extract.python_symbols(
                    (FIXTURES / "module_sample.py").read_bytes(),
                    repository_id="repo-a",
                    path="pkg/mod.py",
                    commit=COMMIT,
                )
            ],
        }
    )
    assert len(code.evidence_ids) == 5
    resolved = open_evidence(
        lab,
        CTX,
        source_id="module:media",
        record_id="repo-a.mod",
        revision_id=code.revision.revision_id,
        locator=CodeLocator(
            repository_id="repo-a", commit=COMMIT, path="pkg/mod.py", symbol="render"
        ),
    )
    assert resolved.verification == VerificationState.VERIFIED
    assert resolved.result.content == 'def render():\n    return "module-level"\n'
    pdf = open_evidence(
        lab,
        CTX,
        source_id="module:media",
        record_id="repo-a.mod",
        revision_id=code.revision.revision_id,
        locator=PdfLocator(page=1),
    )
    assert pdf.verification == VerificationState.UNSUPPORTED and pdf.result.content is None


def test_binary_media_without_locator_is_unsupported(lab: EvidenceStore) -> None:
    result = lab.ingest(
        {
            "source_id": "module:media",
            "record_id": "scan-1",
            "logical_path": "scan.pdf",
            "media_type": extract.MEDIA_PDF,
            "content": b"%PDF-1.7 binary",
        }
    )
    assert result.evidence_ids == []
    res = open_evidence(
        lab,
        CTX,
        source_id="module:media",
        record_id="scan-1",
        revision_id=result.revision.revision_id,
    )
    assert res.verification == VerificationState.UNSUPPORTED


def test_code_locator_must_name_the_record_path(lab: EvidenceStore) -> None:
    result = lab.ingest(
        {
            "source_id": "module:media",
            "record_id": "repo-a.other",
            "logical_path": "repo-a/pkg/mod.py",
            "media_type": extract.MEDIA_PYTHON,
            "content": (FIXTURES / "module_sample.py").read_bytes(),
            "upstream_version": COMMIT,
        }
    )

    def open_path(path: str) -> VerificationState:
        locator = CodeLocator(repository_id="repo-a", commit=COMMIT, path=path, symbol="helper")
        return open_evidence(
            lab,
            CTX,
            source_id="module:media",
            record_id="repo-a.other",
            revision_id=result.revision.revision_id,
            locator=locator,
        ).verification

    assert open_path("pkg/mod.py") == VerificationState.VERIFIED
    assert open_path("pkg/elsewhere.py") == VerificationState.UNSUPPORTED
