"""Citation production: claim validation, Markdown/CSL-JSON golden files, scope denial.

Golden files live in ``tests/fixtures/evidence/``; regenerate intentionally with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_citations.py
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.knowledge.contracts import Bibliographic, TextLocator
from knowledge_engine.knowledge.errors import DeniedError, InvalidInputError
from knowledge_engine.knowledge.evidence import citations
from knowledge_engine.knowledge.evidence.citations import (
    CitationExportRequest,
    CitationRequest,
    export_citations,
    parse_request,
    validate_claims,
)
from knowledge_engine.knowledge.evidence.store import EvidenceStore
from knowledge_engine.knowledge.store import KnowledgeStore

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
PAPER = b"# Findings\n\nRun the tool with care.\n\nResults vary.\n"
NOTE = b"A short note about the tool.\n"
QUOTE = "Run the tool with care."


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[dict[str, Any]]:
    store = KnowledgeStore.open(tmp_path / "lab.db")
    store.create_workspace("Lab", workspace_id="lab")
    store.create_project("lab", "Thesis", project_id="thesis")
    store.register_source("lab", "module:papers", kind="module", display_name="Papers")
    ev = EvidenceStore(store)
    paper = ev.ingest(
        {
            "source_id": "module:papers",
            "record_id": "paper-1",
            "logical_path": "papers/findings.md",
            "canonical_uri": "https://example.org/a b?x=<1>",
            "title": "Deep *Learning* [draft] <v2>",
            "media_type": "text/markdown",
            "content": PAPER,
            "bibliographic": Bibliographic(
                authors=("Ada Lovelace", "Alan Turing"),
                published="2024-05",
                publisher="Test Press",
                container_title="Journal of Tests",
                identifiers={"doi": "10.1000/xyz"},
            ),
        }
    )
    store.classify("module:papers", "paper-1", research_type="paper")
    note = ev.ingest(
        {
            "source_id": "module:papers",
            "record_id": "note-1",
            "logical_path": "notes/n.md",
            "canonical_uri": "javascript:alert(1)",
            "media_type": "text/plain",
            "content": NOTE,
        }
    )
    try:
        yield {"ev": ev, "paper": paper, "note": note, "store": store}
    finally:
        store.close()


def _quote_locator() -> dict[str, Any]:
    start = PAPER.index(QUOTE.encode())
    return {"kind": "text", "start_byte": start, "end_byte": start + len(QUOTE.encode())}


def _claims(lab: dict[str, Any]) -> list[dict[str, Any]]:
    paper_rev = lab["paper"].revision.revision_id
    paper_ref = {
        "source_id": "module:papers",
        "record_id": "paper-1",
        "revision_id": paper_rev,
        "locator": _quote_locator(),
    }
    return [
        {"claim_id": "q1", "kind": "quotation", "text": "The paper warns", "quote": QUOTE,
         "evidence": [paper_ref]},
        {"claim_id": "q2", "kind": "quotation", "quote": "Run the tool carelessly.",
         "evidence": [paper_ref]},
        {"claim_id": "p1", "kind": "paraphrase", "text": "Care is advised when using the tool.",
         "evidence": [paper_ref, {"evidence_id": lab["note"].evidence_ids[0]}]},
        {"claim_id": "u1", "kind": "paraphrase", "text": "Nothing supports this."},
        {"claim_id": "x1", "kind": "paraphrase", "text": "Cites something unknown.",
         "evidence": [{"evidence_id": "ev_" + "0" * 32}]},
    ]  # fmt: skip


def _request(lab: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"context": {"workspace_id": "lab"}, "claims": _claims(lab), **extra}


def test_validation_reports_each_claim(lab: dict[str, Any]) -> None:
    report = validate_claims(lab["ev"], CitationRequest.model_validate(_request(lab)))
    assert "not proof" in report.notice
    by_id = {c.claim_id: c for c in report.claims}
    assert by_id["q1"].status == "resolved" and by_id["q1"].evidence[0].quote_matches
    assert by_id["q2"].status == "unresolved" and by_id["q2"].evidence[0].quote_matches is False
    assert by_id["p1"].status == "resolved" and by_id["p1"].evidence[0].quote_matches is None
    assert by_id["u1"].status == "unsupported"
    assert by_id["x1"].status == "unresolved"
    assert by_id["x1"].evidence[0].error is not None
    assert by_id["x1"].evidence[0].error.code == "unknown_scope"
    assert report.quotations == ["q1", "q2"] and report.paraphrases == ["p1", "u1", "x1"]
    # Deduplicated by evidence identity: q1, q2 and p1 cite the same paper span.
    assert len(report.citations) == 2
    assert by_id["q1"].evidence[0].reference == by_id["p1"].evidence[0].reference == 1
    summary = report.summary
    assert (summary.resolved, summary.unresolved, summary.unsupported) == (2, 2, 1)


def test_citation_identity_is_the_evidence_identity(lab: dict[str, Any]) -> None:
    first = validate_claims(lab["ev"], CitationRequest.model_validate(_request(lab)))
    second = validate_claims(lab["ev"], CitationRequest.model_validate(_request(lab)))
    assert [c.evidence_id for c in first.citations] == [c.evidence_id for c in second.citations]
    # The validated span was remembered: it is now citable by evidence id alone.
    paper_ev = first.citations[0].evidence_id
    by_id = validate_claims(
        lab["ev"],
        CitationRequest.model_validate(
            {
                "context": {"workspace_id": "lab"},
                "claims": [
                    {
                        "claim_id": "a",
                        "kind": "quotation",
                        "quote": QUOTE,
                        "evidence": [{"evidence_id": paper_ev}],
                    },
                ],
            }
        ),
    )
    assert by_id.claims[0].status == "resolved"


def test_stale_and_unavailable_evidence(lab: dict[str, Any]) -> None:
    ev: EvidenceStore = lab["ev"]
    ev.ingest(
        {
            "source_id": "module:papers",
            "record_id": "paper-1",
            "logical_path": "papers/findings.md",
            "media_type": "text/markdown",
            "content": PAPER + b"\nAddendum.\n",
        }
    )
    report = validate_claims(ev, CitationRequest.model_validate(_request(lab)))
    q1 = next(c for c in report.claims if c.claim_id == "q1")
    assert q1.status == "resolved" and any("stale" in issue for issue in q1.issues)
    assert report.summary.stale_references == 1
    ev.delete_record("module:papers", "note-1", reason="rights request")
    report = validate_claims(ev, CitationRequest.model_validate(_request(lab)))
    p1 = next(c for c in report.claims if c.claim_id == "p1")
    assert p1.status == "unresolved"
    assert p1.evidence[1].verification is not None
    assert p1.evidence[1].verification.value == "unavailable"
    exported = export_citations(ev, CitationExportRequest.model_validate(_request(lab)))
    assert exported.markdown is not None and ": unavailable." in exported.markdown


def _golden(name: str, rendered: str) -> None:
    path = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.write_text(rendered, encoding="utf-8")
    assert path.is_file(), f"missing golden file {name}; run with UPDATE_SNAPSHOTS=1"
    assert rendered == path.read_text(encoding="utf-8"), (
        f"{name} changed; if intended, regenerate with UPDATE_SNAPSHOTS=1"
    )


def test_markdown_and_csl_json_golden_files(lab: dict[str, Any]) -> None:
    result = export_citations(lab["ev"], CitationExportRequest.model_validate(_request(lab)))
    assert result.markdown is not None and result.csl_json is not None
    _golden("citations.md", result.markdown)
    _golden("citations.csl.json", json.dumps(result.csl_json, indent=2, ensure_ascii=False) + "\n")
    paper, note = result.csl_json
    assert paper["type"] == "article-journal" and paper["issued"] == {"date-parts": [[2024, 5]]}
    assert paper["DOI"] == "10.1000/xyz"
    # Missing metadata stays absent.
    assert "author" not in note and "issued" not in note and "title" not in note
    assert "URL" not in note  # only http(s) URLs are exported as links
    assert paper["URL"] == "https://example.org/a%20b?x=%3C1%3E"
    # Deterministic: a second export is byte-identical.
    again = export_citations(lab["ev"], CitationExportRequest.model_validate(_request(lab)))
    assert again.markdown == result.markdown and again.csl_json == result.csl_json


def test_export_formats(lab: dict[str, Any]) -> None:
    md = export_citations(
        lab["ev"], CitationExportRequest.model_validate(_request(lab, format="markdown"))
    )
    assert md.markdown is not None and md.csl_json is None
    csl = export_citations(
        lab["ev"], CitationExportRequest.model_validate(_request(lab, format="csl-json"))
    )
    assert csl.markdown is None and csl.csl_json is not None


def test_export_rechecks_scope(lab: dict[str, Any]) -> None:
    lab["store"].classify("module:papers", "paper-1", project_id="thesis")
    request = _request(lab)
    request["context"] = {"workspace_id": "lab", "project_id": "thesis"}
    # The note is workspace-wide, outside a project-only scope: the whole export is denied.
    with pytest.raises(DeniedError):
        export_citations(lab["ev"], CitationExportRequest.model_validate(request))
    request["context"]["include_workspace"] = True
    assert export_citations(lab["ev"], CitationExportRequest.model_validate(request)).citations


def test_escaping_helpers() -> None:
    assert citations.escape_markdown("a *b* [c]\n<d>") == r"a \*b\* \[c\] \<d\>"
    assert citations.markdown_url("https://x.org/a b<c>") == "<https://x.org/a%20b%3Cc%3E>"
    assert citations.safe_url("ftp://x.org/") is None
    assert citations.markdown_url("javascript:alert(1)") == r"javascript:alert\(1\)"
    assert citations.locator_label(TextLocator(heading="H", start_byte=1, end_byte=5)) == (
        "bytes [1, 5), section “H”"
    )


@pytest.mark.parametrize(
    "claims",
    [
        [{"claim_id": "a", "kind": "quotation", "evidence": []}],
        [{"claim_id": "a", "kind": "paraphrase", "text": "x", "quote": "y"}],
        [{"claim_id": "a", "text": "x"}, {"claim_id": "a", "text": "y"}],
        [{"claim_id": "a", "text": "x", "evidence": [{"source_id": "module:papers"}]}],
        [],
    ],
)
def test_invalid_requests(claims: list[dict[str, Any]]) -> None:
    with pytest.raises(InvalidInputError):
        parse_request(CitationRequest, {"context": {"workspace_id": "lab"}, "claims": claims})


def test_unknown_workspace_fails_the_request(lab: dict[str, Any]) -> None:
    from knowledge_engine.knowledge.errors import UnknownScopeError

    request = _request(lab)
    request["context"] = {"workspace_id": "nope"}
    with pytest.raises(UnknownScopeError):
        validate_claims(lab["ev"], CitationRequest.model_validate(request))
