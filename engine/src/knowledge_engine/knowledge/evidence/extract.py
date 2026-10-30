"""Typed locators: extraction from revision bytes, and exact slicing of a locator.

Conventions (ADR 0007; every range is half-open ``[start, end)``):

* **Markdown sections** (:func:`markdown_sections`): one span per ATX heading
  (``#`` to ``######`` followed by a space or the end of the line, outside
  fenced code blocks). A section starts at the first byte of its heading line
  and ends at the first byte of the next heading line of *any* level, or at
  the end of the revision. Offsets are UTF-8 **byte** offsets into the exact
  retained bytes; ``heading`` is the heading text (informational). Text before
  the first heading is not a section.
* **Plain text** (:func:`text_span`, :func:`find_text`, :func:`whole_document`):
  byte offsets that must fall on UTF-8 character boundaries.
* **Transcripts** (:func:`transcript_segments`): a JSON artifact
  ``{"segments": [{"start_ms", "end_ms", "speaker"?, "text"}]}`` (media type
  :data:`MEDIA_TRANSCRIPT`). A locator selects every segment lying inside
  ``[start_ms, end_ms)`` and must align with the first segment's start and the
  last segment's end; its excerpt is the selected texts joined by ``"\\n"``.
* **Code** (:func:`python_symbols`, :func:`snapshot_digest`): 1-based lines
  with an exclusive end; a dirty tree is addressed by a snapshot digest over
  its files, never by a commit.
* **PDF** (:func:`pdf_pages`): no PDF text extractor ships with the engine, so
  page positions cannot be recovered; the result is an ``unsupported`` locator
  with the reason, never an invented page.

The excerpt digest of a span is ``sha256`` of its excerpt bytes.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ..contracts import (
    CodeLocator,
    ExtractionInfo,
    PdfLocator,
    TextLocator,
    TranscriptLocator,
    UnsupportedLocator,
    canonical_json,
    sha256_digest,
)
from ..errors import InvalidInputError

MEDIA_MARKDOWN = "text/markdown"
MEDIA_TEXT = "text/plain"
MEDIA_RST = "text/x-rst"
MEDIA_PYTHON = "text/x-python"
MEDIA_PDF = "application/pdf"
MEDIA_TRANSCRIPT = "application/vnd.knowledge-engine.transcript+json"

#: Media types whose bytes are UTF-8 text.
TEXT_MEDIA_PREFIXES = ("text/",)
TEXT_MEDIA_TYPES = frozenset({"application/json", MEDIA_TRANSCRIPT})

#: Corpus suffix -> media type.
SUFFIX_MEDIA = {".md": MEDIA_MARKDOWN, ".txt": MEDIA_TEXT, ".rst": MEDIA_RST, ".py": MEDIA_PYTHON}

#: Upper bound on the spans one extraction returns.
MAX_SPANS = 2000
#: Upper bound on transcript segments in one artifact.
MAX_SEGMENTS = 100_000

MARKDOWN_EXTRACTION = ExtractionInfo(method="markdown-heading", version="1")
TEXT_EXTRACTION = ExtractionInfo(method="text-document", version="1")
QUOTE_EXTRACTION = ExtractionInfo(method="text-quote", version="1")
TRANSCRIPT_EXTRACTION = ExtractionInfo(method="transcript-segment", version="1")
PYTHON_EXTRACTION = ExtractionInfo(method="python-ast-symbol", version="1")
CITATION_EXTRACTION = ExtractionInfo(method="citation-locator", version="1")

_HEADING_RE = re.compile(rb"^(#{1,6})(?:[ \t]+(.*?))?[ \t#]*\r?$")
_FENCE_RE = re.compile(rb"^ {0,3}(`{3,}|~{3,})")


def is_text_media(media_type: str) -> bool:
    """Whether bytes of ``media_type`` are UTF-8 text."""
    base = media_type.split(";", 1)[0].strip().lower()
    return base.startswith(TEXT_MEDIA_PREFIXES) or base in TEXT_MEDIA_TYPES


@dataclass(frozen=True)
class ExtractedSpan:
    """A locator recovered from revision bytes, its exact excerpt and how it was found."""

    locator: BaseModel
    excerpt: bytes
    extraction: ExtractionInfo

    @property
    def excerpt_digest(self) -> str:
        return sha256_digest(self.excerpt)


@dataclass(frozen=True)
class SliceResult:
    """The outcome of slicing a locator out of revision bytes.

    ``status`` is ``ok`` (``excerpt`` set), ``unsupported`` (no usable
    position) or ``ambiguous`` (several matches); ``reason`` explains a
    non-``ok`` status.
    """

    status: str
    excerpt: bytes | None = None
    reason: str | None = None

    @classmethod
    def ok(cls, excerpt: bytes) -> SliceResult:
        return cls("ok", excerpt)

    @classmethod
    def unsupported(cls, reason: str) -> SliceResult:
        return cls("unsupported", None, reason)

    @classmethod
    def ambiguous(cls, reason: str) -> SliceResult:
        return cls("ambiguous", None, reason)


def _is_boundary(data: bytes, offset: int) -> bool:
    """``offset`` is the start of a UTF-8 character (or the end of ``data``)."""
    return offset == len(data) or (data[offset] & 0xC0) != 0x80


# ── Markdown and plain text ─────────────────────────────────────────


def markdown_sections(data: bytes) -> list[ExtractedSpan]:
    """One :class:`TextLocator` span per ATX heading (see the module conventions)."""
    starts: list[tuple[int, str]] = []
    offset = 0
    fence: bytes | None = None
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\n")
        fence_match = _FENCE_RE.match(body)
        if fence_match is not None:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[:1] * 3
            elif marker.startswith(fence):
                fence = None
        elif fence is None:
            match = _HEADING_RE.match(body)
            if match is not None:
                text = (match.group(2) or b"").decode("utf-8", errors="replace").strip()
                starts.append((offset, text[:500]))
        offset += len(line)
        if len(starts) > MAX_SPANS:
            break
    spans: list[ExtractedSpan] = []
    for index, (start, heading) in enumerate(starts[:MAX_SPANS]):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(data)
        locator = TextLocator(heading=heading or None, start_byte=start, end_byte=end)
        spans.append(ExtractedSpan(locator, data[start:end], MARKDOWN_EXTRACTION))
    return spans


def whole_document(data: bytes) -> ExtractedSpan:
    """The whole revision as one text span."""
    return ExtractedSpan(TextLocator(start_byte=0, end_byte=len(data)), data, TEXT_EXTRACTION)


def text_span(
    data: bytes, start_byte: int, end_byte: int, *, heading: str | None = None
) -> ExtractedSpan:
    """A validated text span ``[start_byte, end_byte)`` of ``data``."""
    result = slice_text(
        data, TextLocator(heading=heading, start_byte=start_byte, end_byte=end_byte)
    )
    if result.excerpt is None:
        raise InvalidInputError(result.reason or "invalid text span")
    locator = TextLocator(heading=heading, start_byte=start_byte, end_byte=end_byte)
    return ExtractedSpan(locator, result.excerpt, QUOTE_EXTRACTION)


def find_text(data: bytes, quote: str, *, limit: int = 50) -> list[ExtractedSpan]:
    """Every exact occurrence of ``quote`` in ``data`` (UTF-8), at most ``limit``."""
    needle = quote.encode("utf-8")
    if not needle:
        raise InvalidInputError("the quote is empty")
    found: list[ExtractedSpan] = []
    start = data.find(needle)
    while start != -1 and len(found) < limit:
        locator = TextLocator(start_byte=start, end_byte=start + len(needle))
        found.append(ExtractedSpan(locator, needle, QUOTE_EXTRACTION))
        start = data.find(needle, start + 1)
    return found


def slice_text(data: bytes, locator: TextLocator) -> SliceResult:
    """The exact bytes of a text locator, or why they cannot be addressed."""
    if locator.end_byte > len(data):
        return SliceResult.unsupported(
            f"the span ends at byte {locator.end_byte} but the revision has {len(data)} bytes"
        )
    if not (_is_boundary(data, locator.start_byte) and _is_boundary(data, locator.end_byte)):
        return SliceResult.unsupported("the span does not fall on UTF-8 character boundaries")
    return SliceResult.ok(data[locator.start_byte : locator.end_byte])


# ── Transcripts ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None


def parse_transcript(data: bytes) -> list[Segment]:
    """Validate a transcript artifact and return its segments in time order."""
    try:
        payload: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidInputError(f"the transcript artifact is not UTF-8 JSON: {exc}") from exc
    raw = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise InvalidInputError("a transcript artifact is {'segments': [...]} or a list")
    if len(raw) > MAX_SEGMENTS:
        raise InvalidInputError(f"a transcript artifact holds at most {MAX_SEGMENTS} segments")
    segments: list[Segment] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InvalidInputError(f"segment {index} is not an object")
        start, end, text = item.get("start_ms"), item.get("end_ms"), item.get("text")
        speaker = item.get("speaker")
        if not (isinstance(start, int) and isinstance(end, int)) or isinstance(start, bool):
            raise InvalidInputError(f"segment {index}: start_ms/end_ms must be integers")
        if start < 0 or end < start:
            raise InvalidInputError(f"segment {index}: needs 0 <= start_ms <= end_ms")
        if not isinstance(text, str):
            raise InvalidInputError(f"segment {index}: text must be a string")
        if speaker is not None and not isinstance(speaker, str):
            raise InvalidInputError(f"segment {index}: speaker must be a string")
        segments.append(Segment(start, end, text, speaker or None))
    segments.sort(key=lambda seg: (seg.start_ms, seg.end_ms))
    return segments


def transcript_segments(data: bytes) -> list[ExtractedSpan]:
    """One :class:`TranscriptLocator` span per segment of a transcript artifact."""
    spans: list[ExtractedSpan] = []
    for seg in parse_transcript(data)[:MAX_SPANS]:
        locator = TranscriptLocator(start_ms=seg.start_ms, end_ms=seg.end_ms, speaker=seg.speaker)
        spans.append(ExtractedSpan(locator, seg.text.encode("utf-8"), TRANSCRIPT_EXTRACTION))
    return spans


def slice_transcript(data: bytes, locator: TranscriptLocator) -> SliceResult:
    try:
        segments = parse_transcript(data)
    except InvalidInputError as exc:
        return SliceResult.unsupported(exc.message)
    inside = [
        seg for seg in segments if seg.start_ms >= locator.start_ms and seg.end_ms <= locator.end_ms
    ]
    if not inside:
        return SliceResult.unsupported("no transcript segment lies inside the locator's range")
    if inside[0].start_ms != locator.start_ms or max(s.end_ms for s in inside) != locator.end_ms:
        return SliceResult.unsupported("the locator does not align with segment boundaries")
    if locator.speaker is not None and any(seg.speaker != locator.speaker for seg in inside):
        return SliceResult.unsupported("the locator's speaker does not match the segments")
    return SliceResult.ok("\n".join(seg.text for seg in inside).encode("utf-8"))


# ── Code ────────────────────────────────────────────────────────────


def snapshot_digest(files: Mapping[str, bytes]) -> str:
    """Digest of a (dirty) working tree: canonical JSON of ``{path: sha256}``, hashed."""
    listing = {path: sha256_digest(content) for path, content in sorted(files.items())}
    return sha256_digest(canonical_json(listing).encode("utf-8"))


def _python_definitions(source: bytes) -> list[tuple[str, int, int]]:
    """``(qualified name, start line, exclusive end line)`` of every def/class."""
    tree = ast.parse(source)
    found: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                last = child.end_lineno or child.lineno
                found.append((name, first, last + 1))
                visit(child, name + ".")

    visit(tree, "")
    return found


def python_symbols(
    data: bytes,
    *,
    repository_id: str,
    path: str,
    commit: str | None = None,
    snapshot: str | None = None,
) -> list[ExtractedSpan]:
    """One :class:`CodeLocator` span per Python def/class (decorators included)."""
    try:
        definitions = _python_definitions(data)
    except (SyntaxError, ValueError) as exc:
        raise InvalidInputError(f"cannot parse {path} as Python: {exc}") from exc
    lines = data.splitlines(keepends=True)
    spans: list[ExtractedSpan] = []
    for name, start, end in definitions[:MAX_SPANS]:
        locator = CodeLocator(
            repository_id=repository_id,
            commit=commit,
            snapshot_digest=snapshot,
            path=path,
            symbol=name,
            start_line=start,
            end_line=end,
        )
        spans.append(
            ExtractedSpan(locator, b"".join(lines[start - 1 : end - 1]), PYTHON_EXTRACTION)
        )
    return spans


def slice_code(
    data: bytes, locator: CodeLocator, *, media_type: str, upstream_version: str | None
) -> SliceResult:
    """Lines of a code locator; checks the commit/snapshot the revision records."""
    pinned = locator.commit or locator.snapshot_digest
    if upstream_version is None or pinned != upstream_version:
        return SliceResult.unsupported(
            "the locator's commit/snapshot does not match the revision's recorded version"
        )
    lines = data.splitlines(keepends=True)
    start, end = locator.start_line, locator.end_line
    if start is None or end is None:
        if locator.symbol is None or not media_type.startswith(MEDIA_PYTHON):
            return SliceResult.unsupported("a code locator needs lines or a Python symbol")
        try:
            matches = [d for d in _python_definitions(data) if d[0] == locator.symbol]
        except (SyntaxError, ValueError):
            return SliceResult.unsupported("the revision is not parseable Python")
        if not matches:
            return SliceResult.unsupported(f"symbol {locator.symbol!r} is not defined")
        if len(matches) > 1:
            return SliceResult.ambiguous(
                f"symbol {locator.symbol!r} is defined {len(matches)} times; give lines"
            )
        _, start, end = matches[0]
    if end > len(lines) + 1:
        return SliceResult.unsupported(
            f"the range ends at line {end} but the revision has {len(lines)} lines"
        )
    return SliceResult.ok(b"".join(lines[start - 1 : end - 1]))


# ── PDF ─────────────────────────────────────────────────────────────

PDF_UNSUPPORTED_REASON = (
    "no PDF text extractor is available offline; page positions cannot be recovered"
)


def pdf_pages(data: bytes) -> UnsupportedLocator:
    """Always ``unsupported``: the engine ships no PDF text extractor (never invent pages)."""
    del data
    return UnsupportedLocator(reason=PDF_UNSUPPORTED_REASON)


# ── Dispatch ────────────────────────────────────────────────────────


def slice_locator(
    data: bytes, locator: BaseModel, *, media_type: str, upstream_version: str | None = None
) -> SliceResult:
    """The exact excerpt bytes ``locator`` addresses in ``data`` (or why not)."""
    if isinstance(locator, TextLocator):
        if not is_text_media(media_type):
            return SliceResult.unsupported(f"a text locator cannot address {media_type}")
        return slice_text(data, locator)
    if isinstance(locator, TranscriptLocator):
        if not media_type.startswith(MEDIA_TRANSCRIPT):
            return SliceResult.unsupported(f"a transcript locator cannot address {media_type}")
        return slice_transcript(data, locator)
    if isinstance(locator, CodeLocator):
        if not is_text_media(media_type):
            return SliceResult.unsupported(f"a code locator cannot address {media_type}")
        return slice_code(data, locator, media_type=media_type, upstream_version=upstream_version)
    if isinstance(locator, PdfLocator):
        return SliceResult.unsupported(PDF_UNSUPPORTED_REASON)
    if isinstance(locator, UnsupportedLocator):
        return SliceResult.unsupported(locator.reason)
    return SliceResult.unsupported(f"unknown locator type {type(locator).__name__}")


def default_spans(data: bytes, media_type: str) -> list[ExtractedSpan]:
    """The spans ingestion records for a revision when the producer gives none."""
    base = media_type.split(";", 1)[0].strip().lower()
    try:
        if base == MEDIA_MARKDOWN:
            data.decode("utf-8")
            return markdown_sections(data) or [whole_document(data)]
        if base == MEDIA_TRANSCRIPT:
            return transcript_segments(data)
        if base in (MEDIA_TEXT, MEDIA_RST):
            data.decode("utf-8")
            return [whole_document(data)]
    except (UnicodeDecodeError, InvalidInputError):
        return []
    return []


__all__ = [
    "CITATION_EXTRACTION",
    "MARKDOWN_EXTRACTION",
    "MEDIA_MARKDOWN",
    "MEDIA_PDF",
    "MEDIA_PYTHON",
    "MEDIA_RST",
    "MEDIA_TEXT",
    "MEDIA_TRANSCRIPT",
    "PDF_UNSUPPORTED_REASON",
    "PYTHON_EXTRACTION",
    "QUOTE_EXTRACTION",
    "SUFFIX_MEDIA",
    "TEXT_EXTRACTION",
    "TRANSCRIPT_EXTRACTION",
    "ExtractedSpan",
    "Segment",
    "SliceResult",
    "default_spans",
    "find_text",
    "is_text_media",
    "markdown_sections",
    "parse_transcript",
    "pdf_pages",
    "python_symbols",
    "slice_code",
    "slice_locator",
    "slice_text",
    "slice_transcript",
    "snapshot_digest",
    "text_span",
    "transcript_segments",
    "whole_document",
]
