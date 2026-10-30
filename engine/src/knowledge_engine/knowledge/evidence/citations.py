"""Citation production: claim-to-evidence validation, Markdown and CSL-JSON export (ADR 0013).

A caller sends a claim-to-evidence map: each claim is a ``quotation`` (with
the exact quoted passage) or a ``paraphrase``, and cites evidence either by a
stored ``evidence_id`` or by ``source_id`` + ``record_id`` + ``revision_id``
+ ``locator``. :func:`validate_claims`:

* rechecks the caller's scope for every cited record (any evidence outside it
  fails the whole request with ``denied``, so no metadata leaks);
* resolves every reference against its exact revision
  (:func:`~knowledge_engine.knowledge.evidence.resolve.open_evidence`);
* reports a claim ``resolved`` when all its evidence is ``verified`` or
  ``stale`` (stale is flagged), ``unresolved`` when some evidence does not
  resolve, and ``unsupported`` when it cites nothing;
* requires a quotation to equal each cited span **exactly**; paraphrases are
  not compared with the text.

This checks that evidence resolves and that quotations are verbatim. It is
**not** proof that the evidence entails a claim; every report says so.

Citation identity is the evidence identity
(:func:`~knowledge_engine.knowledge.contracts.derive_evidence_id`: revision +
locator + excerpt digest), so it survives reindexing and never uses row ids.
:func:`export_citations` renders the deduplicated references deterministically
as Markdown (inline ``[n]`` references and a reference list; titles escaped,
only ``http(s)`` URLs linked) and as CSL-JSON (absent author/date stay absent).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..contracts import (
    Citation,
    CitationExport,
    CodeLocator,
    EffectiveScope,
    ErrorInfo,
    EvidenceId,
    Locator,
    PdfLocator,
    RecordId,
    ResearchType,
    RevisionId,
    SourceId,
    SourceRecord,
    TextLocator,
    TranscriptLocator,
    UnsupportedLocator,
    VerificationState,
    parse_query_context,
    validation_messages,
)
from ..errors import DeniedError, InvalidInputError, KnowledgeError
from ..scope import DEFAULT_PROFILE_ID
from .extract import CITATION_EXTRACTION, ExtractedSpan
from .resolve import Resolution, open_evidence, open_span
from .store import EvidenceStore

_logger = logging.getLogger(__name__)

#: Printed with every validation and export.
NOTICE = (
    "Validation checks that cited evidence resolves to its exact revision and that "
    "quotations match their spans exactly. It is not proof that the evidence entails "
    "the claims; semantic support must be judged by a reader."
)
MAX_CLAIMS = 500
MAX_REFS_PER_CLAIM = 50
_CLAIM_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
RESOLVED_STATES = frozenset({VerificationState.VERIFIED, VerificationState.STALE})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRef(_Model):
    """A cited evidence span: a stored ``evidence_id``, or an explicit revision + locator."""

    evidence_id: EvidenceId | None = None
    source_id: SourceId | None = None
    record_id: RecordId | None = None
    revision_id: RevisionId | None = None
    locator: Locator | None = None

    @model_validator(mode="after")
    def _one_form(self) -> EvidenceRef:
        explicit = (self.source_id, self.record_id, self.revision_id)
        if self.evidence_id is not None:
            if any(value is not None for value in (*explicit, self.locator)):
                raise ValueError("give either evidence_id or source/record/revision/locator")
        elif any(value is None for value in explicit):
            raise ValueError("an explicit reference needs source_id, record_id and revision_id")
        return self


class Claim(_Model):
    """One claim of a generated answer and the evidence it cites."""

    claim_id: str = Field(pattern=_CLAIM_ID)
    kind: Literal["quotation", "paraphrase"] = "paraphrase"
    text: str = Field(default="", max_length=20_000)
    quote: str | None = Field(
        default=None, max_length=20_000, description="The verbatim passage (quotations only)."
    )
    evidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=MAX_REFS_PER_CLAIM)

    @model_validator(mode="after")
    def _kind_fields(self) -> Claim:
        if self.kind == "quotation" and not self.quote:
            raise ValueError("a quotation needs the quoted passage in 'quote'")
        if self.kind == "paraphrase" and self.quote is not None:
            raise ValueError("a paraphrase has no 'quote'; use kind 'quotation'")
        if self.kind == "paraphrase" and not self.text.strip():
            raise ValueError("a paraphrase needs its text")
        return self


class CitationRequest(_Model):
    """A claim-to-evidence map within one scope."""

    context: dict[str, Any] = Field(
        description="A QueryContext (workspace_id required); every cited record must be in it."
    )
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=MAX_CLAIMS)

    @model_validator(mode="after")
    def _unique_ids(self) -> CitationRequest:
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim ids must be unique")
        return self


class CitationExportRequest(CitationRequest):
    format: Literal["markdown", "csl-json", "both"] = "both"


# ── Outputs ──────────────────────────────────────────────────────────


class EvidenceCheck(BaseModel):
    """How one cited reference resolved."""

    evidence_id: str | None = None
    source_id: str | None = None
    record_id: str | None = None
    revision_id: str | None = None
    verification: VerificationState | None = None
    reason: str | None = None
    current_revision_id: str | None = None
    quote_matches: bool | None = None
    reference: int | None = Field(default=None, description="1-based reference number.")
    error: ErrorInfo | None = None


class ClaimReport(BaseModel):
    claim_id: str
    kind: Literal["quotation", "paraphrase"]
    status: Literal["resolved", "unresolved", "unsupported"]
    issues: list[str] = Field(default_factory=list)
    evidence: list[EvidenceCheck] = Field(default_factory=list)


class ValidationSummary(BaseModel):
    claims: int
    resolved: int
    unresolved: int
    unsupported: int
    references: int
    stale_references: int


class ValidationReport(BaseModel):
    notice: str = NOTICE
    effective_scope: EffectiveScope
    summary: ValidationSummary
    claims: list[ClaimReport]
    quotations: list[str] = Field(description="Claim ids of verbatim quotations.")
    paraphrases: list[str] = Field(description="Claim ids of paraphrases (never compared).")
    citations: list[Citation] = Field(description="Deduplicated by evidence identity, in order.")


class CitationExportResult(BaseModel):
    notice: str = NOTICE
    format: Literal["markdown", "csl-json", "both"]
    markdown: str | None = None
    csl_json: list[dict[str, Any]] | None = None
    summary: ValidationSummary
    claims: list[ClaimReport]
    citations: list[Citation]


# ── Validation ───────────────────────────────────────────────────────


@dataclass
class _Validated:
    report: ValidationReport
    records: dict[str, SourceRecord] = field(default_factory=dict)
    claims: tuple[Claim, ...] = ()


def _resolve(
    evidence: EvidenceStore,
    context: Any,
    ref: EvidenceRef,
    profile: str,
) -> Resolution:
    if ref.evidence_id is not None:
        return open_span(evidence, context, ref.evidence_id, configured_profile=profile)
    return open_evidence(
        evidence,
        context,
        source_id=str(ref.source_id),
        record_id=str(ref.record_id),
        revision_id=str(ref.revision_id),
        locator=ref.locator,
        configured_profile=profile,
    )


def _citation_export(record: SourceRecord) -> CitationExport:
    return CitationExport(
        title=record.title or None,
        authors=record.bibliographic.authors,
        published=record.bibliographic.published,
        uri=record.canonical_uri,
        publisher=record.bibliographic.publisher,
    )


def validate_claims(
    evidence: EvidenceStore,
    request: CitationRequest,
    *,
    configured_profile: str = DEFAULT_PROFILE_ID,
    remember: bool = True,
) -> ValidationReport:
    """Validate a claim-to-evidence map (see the module docstring)."""
    return _validate(evidence, request, configured_profile, remember).report


def _validate(
    evidence: EvidenceStore, request: CitationRequest, profile: str, remember: bool
) -> _Validated:
    context = parse_query_context(request.context)
    scope = evidence.store.resolve(context, configured_profile=profile)
    citations: dict[str, Citation] = {}
    records: dict[str, SourceRecord] = {}
    reports: list[ClaimReport] = []
    stale = 0
    for claim in request.claims:
        checks: list[EvidenceCheck] = []
        issues: list[str] = []
        for ref in claim.evidence:
            check, citation, record = _check_ref(evidence, context, ref, claim, profile, remember)
            if citation is not None and record is not None:
                if citation.evidence_id not in citations:
                    citations[citation.evidence_id] = citation
                    records[citation.evidence_id] = record
                    stale += int(citation.verification == VerificationState.STALE)
                check.reference = list(citations).index(citation.evidence_id) + 1
            checks.append(check)
        if not checks:
            status: Literal["resolved", "unresolved", "unsupported"] = "unsupported"
            issues.append("the claim cites no evidence")
        else:
            status = "resolved"
            for index, check in enumerate(checks, 1):
                if check.error is not None:
                    status = "unresolved"
                    issues.append(f"evidence {index}: {check.error.message}")
                elif check.verification not in RESOLVED_STATES:
                    status = "unresolved"
                    state = check.verification.value if check.verification else "unknown"
                    issues.append(f"evidence {index} is {state}: {check.reason or 'no reason'}")
                elif check.quote_matches is False:
                    status = "unresolved"
                    issues.append(f"evidence {index}: the quote does not match the span exactly")
                elif check.verification == VerificationState.STALE:
                    issues.append(f"evidence {index} is stale: a newer revision exists")
        reports.append(
            ClaimReport(
                claim_id=claim.claim_id,
                kind=claim.kind,
                status=status,
                issues=issues,
                evidence=checks,
            )
        )
    summary = ValidationSummary(
        claims=len(reports),
        resolved=sum(r.status == "resolved" for r in reports),
        unresolved=sum(r.status == "unresolved" for r in reports),
        unsupported=sum(r.status == "unsupported" for r in reports),
        references=len(citations),
        stale_references=stale,
    )
    report = ValidationReport(
        effective_scope=scope,
        summary=summary,
        claims=reports,
        quotations=[c.claim_id for c in request.claims if c.kind == "quotation"],
        paraphrases=[c.claim_id for c in request.claims if c.kind == "paraphrase"],
        citations=list(citations.values()),
    )
    _logger.info(
        "citations: %d claim(s), %d resolved, %d unresolved, %d unsupported",
        summary.claims,
        summary.resolved,
        summary.unresolved,
        summary.unsupported,
    )
    return _Validated(report, records, request.claims)


def _check_ref(
    evidence: EvidenceStore,
    context: Any,
    ref: EvidenceRef,
    claim: Claim,
    profile: str,
    remember: bool,
) -> tuple[EvidenceCheck, Citation | None, SourceRecord | None]:
    try:
        res = _resolve(evidence, context, ref, profile)
    except DeniedError:
        raise
    except KnowledgeError as exc:
        error = ErrorInfo(code=exc.code, message=exc.message, details=exc.details)
        return (
            EvidenceCheck(
                evidence_id=ref.evidence_id,
                source_id=ref.source_id,
                record_id=ref.record_id,
                revision_id=ref.revision_id,
                error=error,
            ),
            None,
            None,
        )
    result = res.result
    evidence_id = res.evidence_id
    if evidence_id is None and result.locator is not None:
        evidence_id = evidence.find_span(result.revision_id, result.locator)
    if (
        remember
        and not evidence.store.readonly
        and evidence_id is not None
        and ref.evidence_id is None
        and result.locator is not None
        and result.content is not None
        and result.verification in RESOLVED_STATES
    ):
        evidence.remember_span(
            result.revision_id,
            ExtractedSpan(result.locator, result.content.encode("utf-8"), CITATION_EXTRACTION),
        )
    quote_matches: bool | None = None
    if claim.kind == "quotation" and result.verification in RESOLVED_STATES:
        quote_matches = result.content == claim.quote
    check = EvidenceCheck(
        evidence_id=evidence_id,
        source_id=result.source_id,
        record_id=result.record_id,
        revision_id=result.revision_id,
        verification=result.verification,
        reason=res.reason,
        current_revision_id=res.current_revision_id,
        quote_matches=quote_matches,
    )
    if evidence_id is None or result.locator is None:
        if result.locator is None and result.verification in RESOLVED_STATES:
            check.reason = "cite a locator: a whole revision has no evidence identity"
            check.verification = VerificationState.UNSUPPORTED
        return check, None, None
    citation = Citation(
        evidence_id=evidence_id,
        source_id=result.source_id,
        record_id=result.record_id,
        revision_id=result.revision_id,
        locator=result.locator,
        export=_citation_export(res.record),
        verification=result.verification,
    )
    return check, citation, res.record


# ── Rendering ────────────────────────────────────────────────────────

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")
_WS = re.compile(r"\s+")


def escape_markdown(text: str) -> str:
    """Inline-safe Markdown text: whitespace collapsed, markup characters escaped."""
    return _MD_SPECIAL.sub(r"\\\1", _WS.sub(" ", text).strip())


_URL_UNSAFE = {" ": "%20", "\t": "%09", "\n": "%0A", "\r": "%0D", "<": "%3C", ">": "%3E",
               '"': "%22", "`": "%60"}  # fmt: skip


def safe_url(url: str) -> str | None:
    """An ``http(s)`` URL with unsafe characters percent-encoded; ``None`` for other schemes."""
    cleaned = url.strip()
    if not re.match(r"^https?://", cleaned, flags=re.IGNORECASE):
        return None
    return "".join(_URL_UNSAFE.get(char, char) for char in cleaned)


def markdown_url(url: str) -> str:
    """A safe Markdown autolink for http(s) URLs; any other URI as escaped text."""
    safe = safe_url(url)
    return f"<{safe}>" if safe is not None else escape_markdown(url)


def _ms(value: int) -> str:
    minutes, rest = divmod(value, 60_000)
    seconds, millis = divmod(rest, 1000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def locator_label(locator: Any) -> str:
    """A short, deterministic human label of a locator."""
    if isinstance(locator, TextLocator):
        label = f"bytes [{locator.start_byte}, {locator.end_byte})"
        return f"{label}, section “{locator.heading}”" if locator.heading else label
    if isinstance(locator, TranscriptLocator):
        label = f"time [{_ms(locator.start_ms)}, {_ms(locator.end_ms)})"
        return f"{label}, speaker {locator.speaker}" if locator.speaker else label
    if isinstance(locator, CodeLocator):
        pinned = (
            f"commit {locator.commit[:12]}"
            if locator.commit
            else f"snapshot {str(locator.snapshot_digest)[:19]}"
        )
        where = locator.path
        if locator.start_line is not None and locator.end_line is not None:
            where += f" lines [{locator.start_line}, {locator.end_line})"
        if locator.symbol:
            where += f" ({locator.symbol})"
        return f"{where} at {pinned}"
    if isinstance(locator, PdfLocator):
        return f"page {locator.page}"
    if isinstance(locator, UnsupportedLocator):
        return "no locator"
    return "whole revision"


def _reference_line(number: int, citation: Citation, record: SourceRecord) -> str:
    parts: list[str] = []
    export = citation.export
    parts.append(escape_markdown(export.title) + "." if export.title else "(untitled).")
    if export.authors:
        parts.append(escape_markdown("; ".join(export.authors)) + ".")
    if export.published:
        parts.append(escape_markdown(export.published) + ".")
    if record.bibliographic.container_title:
        parts.append("*" + escape_markdown(record.bibliographic.container_title) + "*.")
    if export.publisher:
        parts.append(escape_markdown(export.publisher) + ".")
    if export.uri:
        parts.append(markdown_url(export.uri) + ".")
    where = f"`{citation.source_id}`"
    if record.logical_path:
        where += " " + escape_markdown(record.logical_path)
    parts.append(
        f"{where}, revision `{citation.revision_id}`, "
        f"`{_WS.sub(' ', locator_label(citation.locator)).replace('`', chr(39))}`."
    )
    parts.append(f"Evidence `{citation.evidence_id}`: {citation.verification.value}.")
    return f"[{number}] " + " ".join(parts)


def render_markdown(validated: _Validated) -> str:
    """Deterministic Markdown: quotations, paraphrases and unsupported claims, then references."""
    report = validated.report
    by_id = {claim.claim_id: claim for claim in validated.claims}
    lines: list[str] = []

    def refs(claim_report: ClaimReport) -> str:
        numbers = sorted({c.reference for c in claim_report.evidence if c.reference is not None})
        marks = "".join(f"[{n}]" for n in numbers)
        unresolved = any(
            c.reference is None or c.verification not in RESOLVED_STATES or c.quote_matches is False
            for c in claim_report.evidence
        )
        return marks + ("[unresolved]" if unresolved else "")

    quotations = [r for r in report.claims if r.kind == "quotation" and r.status != "unsupported"]
    paraphrases = [r for r in report.claims if r.kind == "paraphrase" and r.status != "unsupported"]
    unsupported = [r for r in report.claims if r.status == "unsupported"]
    if quotations:
        lines += ["## Quotations", ""]
        for item in quotations:
            claim = by_id[item.claim_id]
            quote = _WS.sub(" ", claim.quote or "").strip().replace("”", "\\”")
            lead = f"{claim.text.strip()}: " if claim.text.strip() else ""
            lines.append(f"- {lead}“{quote}” {refs(item)}".rstrip())
        lines.append("")
    if paraphrases:
        lines += ["## Paraphrases", ""]
        for item in paraphrases:
            claim = by_id[item.claim_id]
            lines.append(f"- {_WS.sub(' ', claim.text).strip()} {refs(item)}".rstrip())
        lines.append("")
    if unsupported:
        lines += ["## Claims without evidence", ""]
        for item in unsupported:
            claim = by_id[item.claim_id]
            text = _WS.sub(" ", claim.text or claim.quote or "").strip()
            lines.append(f"- {text} [no evidence]")
        lines.append("")
    lines += ["## References", ""]
    if not report.citations:
        lines.append("_No resolvable evidence._")
    for number, citation in enumerate(report.citations, 1):
        # One paragraph per reference.
        lines += [_reference_line(number, citation, validated.records[citation.evidence_id]), ""]
    if not report.citations:
        lines.append("")
    lines += [f"_{NOTICE}_", ""]
    return "\n".join(lines)


_CSL_TYPES = {
    ResearchType.PAPER: "article-journal",
    ResearchType.ARTICLE: "article",
    ResearchType.DATASET: "dataset",
    ResearchType.CODE: "software",
    ResearchType.REFERENCE: "entry",
}
_CSL_IDS = {"doi": "DOI", "isbn": "ISBN", "issn": "ISSN", "pmid": "PMID", "pmcid": "PMCID"}
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def _csl_date(value: str) -> dict[str, Any]:
    match = _DATE_RE.match(value.strip())
    if match is None:
        return {"raw": value}
    return {"date-parts": [[int(part) for part in match.groups() if part is not None]]}


def csl_item(citation: Citation, record: SourceRecord) -> dict[str, Any]:
    """One CSL-JSON item; absent metadata stays absent (never invented)."""
    item: dict[str, Any] = {
        "id": citation.evidence_id,
        "type": _CSL_TYPES.get(record.research_type, "document"),
    }
    export = citation.export
    if export.title:
        item["title"] = export.title
    if export.authors:
        item["author"] = [{"literal": name} for name in export.authors]
    if export.published:
        item["issued"] = _csl_date(export.published)
    if record.bibliographic.container_title:
        item["container-title"] = record.bibliographic.container_title
    if export.publisher:
        item["publisher"] = export.publisher
    url = safe_url(export.uri) if export.uri else None
    if url is not None:
        item["URL"] = url
    for key, value in sorted(record.bibliographic.identifiers.items()):
        mapped = _CSL_IDS.get(key.lower())
        if mapped is not None:
            item[mapped] = value
    item["note"] = (
        f"{citation.source_id} {citation.record_id} revision {citation.revision_id}; "
        f"{locator_label(citation.locator)}; verification {citation.verification.value}"
    )
    item["custom"] = {
        "evidence_id": citation.evidence_id,
        "source_id": citation.source_id,
        "record_id": citation.record_id,
        "revision_id": citation.revision_id,
        "canonical_uri": export.uri,
        "locator": citation.locator.model_dump(mode="json"),
        "verification": citation.verification.value,
    }
    return item


def export_citations(
    evidence: EvidenceStore,
    request: CitationExportRequest,
    *,
    configured_profile: str = DEFAULT_PROFILE_ID,
    remember: bool = True,
) -> CitationExportResult:
    """Validate (rechecking scope) and render the references deterministically."""
    validated = _validate(evidence, request, configured_profile, remember)
    report = validated.report
    markdown = render_markdown(validated) if request.format in ("markdown", "both") else None
    csl = (
        [csl_item(c, validated.records[c.evidence_id]) for c in report.citations]
        if request.format in ("csl-json", "both")
        else None
    )
    return CitationExportResult(
        format=request.format,
        markdown=markdown,
        csl_json=csl,
        summary=report.summary,
        claims=report.claims,
        citations=report.citations,
    )


def parse_request[M: BaseModel](model: type[M], payload: Any) -> M:
    """Validate a citation request with typed ``invalid_input`` errors."""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise InvalidInputError(
            "invalid citation request", details={"errors": validation_messages(exc)}
        ) from exc


__all__ = [
    "MAX_CLAIMS",
    "NOTICE",
    "CitationExportRequest",
    "CitationExportResult",
    "CitationRequest",
    "Claim",
    "ClaimReport",
    "EvidenceCheck",
    "EvidenceRef",
    "ValidationReport",
    "ValidationSummary",
    "csl_item",
    "escape_markdown",
    "export_citations",
    "locator_label",
    "markdown_url",
    "parse_request",
    "render_markdown",
    "safe_url",
    "validate_claims",
]
