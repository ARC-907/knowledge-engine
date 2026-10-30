"""Module-ingested evidence is searchable and openable through the retrieval broker (B-030)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.knowledge.broker import KnowledgeBroker
from knowledge_engine.knowledge.contracts import (
    RecordScope,
    SourceState,
    TextLocator,
    VerificationState,
    parse_query_context,
)
from knowledge_engine.knowledge.errors import UnavailableError, UnknownScopeError
from knowledge_engine.knowledge.evidence.store import EvidenceStore
from knowledge_engine.knowledge.sources import build_adapter_factory
from knowledge_engine.knowledge.sources.evidence import EvidenceSourceAdapter
from knowledge_engine.knowledge.store import KnowledgeStore
from knowledge_engine.modules.artifacts import ArtifactPayload, NormalizedArtifact
from knowledge_engine.modules.evidence_ingestion import EvidenceStoreIngestion
from knowledge_engine.modules.ingestion import DerivationInput

SOURCE = "module:example.news"
ARTICLES = {
    "solar": (
        "text/html",
        b"<html><body><h1>Grid storage</h1><p>Solar farms pair with batteries.</p></body></html>",
    ),
    "tides": ("text/plain", b"Tidal energy depends on the moon and the harbour."),
    "binary": ("application/octet-stream", b"\x00\x01solar\x02"),
}


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[KnowledgeStore, EvidenceStore]]:
    with KnowledgeStore.open(tmp_path / "knowledge.db") as store:
        store.ensure_workspace("default", "Default")
        store.create_project("default", "Energy", project_id="energy")
        yield store, EvidenceStore(store)


def _payload(name: str, run_id: str = "run_1") -> ArtifactPayload:
    media_type, content = ARTICLES[name]
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return ArtifactPayload(
        artifact=NormalizedArtifact(
            artifact_id="art_" + hashlib.sha256(name.encode()).hexdigest()[:32],
            module_id="example.news",
            module_version="1.0.0",
            run_id=run_id,
            scope=RecordScope(workspace_id="default"),
            media_type=media_type,
            digest=digest,
            byte_length=len(content),
            canonical_uri=f"https://news.example.org/{name}",
            title=name.title(),
            created_at=datetime.now(UTC),
        ),
        content=content,
    )


def _ingest(
    evidence: EvidenceStore, names: list[str], project: str | None = None
) -> dict[str, Any]:
    adapter = EvidenceStoreIngestion(evidence, default_workspace="default")
    receipt = adapter.ingest(
        [_payload(n) for n in names],
        destination=RecordScope(workspace_id="default", project_id=project),
        derivation=DerivationInput(
            run_id="run_1",
            module_id="example.news",
            module_version="1.0.0",
            parameters_digest="sha256:" + "0" * 64,
        ),
    )
    return receipt.model_dump(mode="json")


def _broker(store: KnowledgeStore) -> KnowledgeBroker:
    return KnowledgeBroker(store, build_adapter_factory(None, evidence=EvidenceStore(store)))


def _search(broker: KnowledgeBroker, query: str, **context: Any) -> Any:
    return broker.search(parse_query_context({"workspace_id": "default", **context}), query)


def test_module_evidence_is_searchable_and_the_result_is_not_partial(
    stores: tuple[KnowledgeStore, EvidenceStore],
) -> None:
    store, evidence = stores
    _ingest(evidence, ["solar", "tides", "binary"])
    with closing(_broker(store)) as broker:
        envelope = _search(broker, "solar batteries")
    assert envelope.partial is False
    (status,) = envelope.source_status
    assert (status.source_id, status.state) == (SOURCE, SourceState.OK)
    assert [hit.title for hit in envelope.hits] == ["Solar"]  # binary content is not text
    hit = envelope.hits[0]
    assert hit.revision_id is not None and hit.revision_id.startswith("rev_")
    assert "Solar farms pair with batteries" in (hit.excerpt or "")
    assert "<p>" not in (hit.excerpt or "")
    assert hit.provenance_id == "run:run_1"
    assert hit.ranking.score_kind == "bm25"


def test_hybrid_falls_back_to_keywords_with_a_warning_and_vector_is_unavailable(
    stores: tuple[KnowledgeStore, EvidenceStore],
) -> None:
    store, evidence = stores
    _ingest(evidence, ["tides"])
    with closing(_broker(store)) as broker:
        hybrid = _search(broker, "moon", mode="hybrid")
        # The only selected source cannot answer vector queries: the request fails
        # with a typed error instead of returning an empty list.
        with pytest.raises(UnavailableError):
            _search(broker, "moon", mode="vector")
    assert [h.title for h in hybrid.hits] == ["Tides"]
    assert any("keyword ranking only" in w for w in hybrid.warnings)


def test_project_filters_apply_before_ranking(stores: tuple[KnowledgeStore, EvidenceStore]) -> None:
    store, evidence = stores
    _ingest(evidence, ["solar"], project="energy")
    _ingest(evidence, ["tides"])
    with closing(_broker(store)) as broker:
        in_project = _search(broker, "solar moon", project_id="energy")
        workspace = _search(broker, "solar moon")
    assert [h.title for h in in_project.hits] == ["Solar"]
    assert sorted(h.title for h in workspace.hits) == ["Solar", "Tides"]


def test_open_serves_the_exact_revision_and_rechecks_scope(
    stores: tuple[KnowledgeStore, EvidenceStore],
) -> None:
    store, evidence = stores
    receipt = _ingest(evidence, ["tides"])
    (revision_id,) = receipt["artifact_revisions"].values()
    info = evidence.get_revision(revision_id)
    assert info is not None
    record_id = info.revision.record_id
    context = parse_query_context({"workspace_id": "default"})
    with closing(_broker(store)) as broker:
        opened = broker.open(context, SOURCE, record_id, revision_id)
        quoted = broker.open(
            context,
            SOURCE,
            record_id,
            revision_id,
            TextLocator(start_byte=0, end_byte=12),
        )
        with pytest.raises(UnknownScopeError):
            broker.open(context, SOURCE, "r-unknown", revision_id)
    assert opened.verification == VerificationState.VERIFIED
    assert opened.content == ARTICLES["tides"][1].decode()
    assert quoted.verification == VerificationState.VERIFIED


def test_adapter_reports_a_closed_store(tmp_path: Path) -> None:
    store = KnowledgeStore.open(tmp_path / "k.db")
    store.ensure_workspace("default", "Default")
    snapshot = store.snapshot(parse_query_context({"workspace_id": "default"}))
    adapter = EvidenceSourceAdapter(SOURCE, "module", EvidenceStore(store), snapshot)
    healthy = adapter.health()
    store.close()
    closed = adapter.health()
    assert healthy.state == SourceState.OK
    assert closed.state == SourceState.UNAVAILABLE
    caps = adapter.describe_capabilities()
    assert caps.modes == ("fts", "hybrid") and caps.supports_open
