"""Module artifacts ingested into the real evidence store (P5/P6 wired to P3)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.core.settings import Settings
from knowledge_engine.knowledge.errors import InvalidInputError, UnavailableError, UnknownScopeError
from knowledge_engine.knowledge.evidence.store import EvidenceStore
from knowledge_engine.knowledge.store import KnowledgeStore
from knowledge_engine.modules.evidence_ingestion import (
    EvidenceStoreIngestion,
    LazyEvidenceIngestion,
    idempotency_key,
    ingestion_from_settings,
    module_source_id_in,
)
from knowledge_engine.modules.ingestion import UnconfiguredIngestion

SCOPE = {"workspace_id": "research", "project_id": "p1"}


@pytest.fixture
def knowledge(mod_env: Any) -> Iterator[KnowledgeStore]:
    with KnowledgeStore.open(mod_env.data / "knowledge.db") as store:
        store.ensure_workspace("default", "Default")
        store.create_workspace("Research", workspace_id="research")
        store.create_project("research", "Project one", project_id="p1")
        store.create_project("research", "Project two", project_id="p2")
        store.create_workspace("Other", workspace_id="other")
        yield store


@pytest.fixture
def evidence(knowledge: KnowledgeStore) -> EvidenceStore:
    return EvidenceStore(knowledge)


@pytest.fixture
def real_host(
    make_host: Callable[..., Any], evidence: EvidenceStore, helpers: Any, mod_env: Any
) -> Any:
    host = make_host(ingestion=EvidenceStoreIngestion(evidence, default_workspace="default"))
    helpers.enable(host, mod_env.news)
    helpers.enable(host, mod_env.transcription)
    host.configure("example.news", {"fixture_roots": ["fixtures", "../transcription/fixtures"]})
    return host


def _fetch(host: Any, mod_env: Any, scope: dict[str, Any] = SCOPE) -> dict[str, Any]:
    url = mod_env.fixture_uri("transcription", "interview.txt")
    result: dict[str, Any] = host.invoke(
        "example.news", "fetch", {"url": url}, scope=scope, ingest=True
    )
    return result


def test_artifacts_become_revisions_with_provenance_and_scope(
    real_host: Any, mod_env: Any, evidence: EvidenceStore, knowledge: KnowledgeStore
) -> None:
    first = _fetch(real_host, mod_env)
    receipt = first["ingestion"]
    artifact_id = first["artifacts"][0]["artifact_id"]
    revision_id = receipt["artifact_revisions"][artifact_id]
    assert receipt["source_id"] == "module:example.news--research"
    assert (receipt["created"], receipt["reused"]) == (1, 0)

    source = knowledge.get_source("module:example.news--research")
    assert (source.workspace_id, source.kind) == ("research", "module")
    info = evidence.get_revision(revision_id)
    assert info is not None
    record = knowledge.get_record("module:example.news--research", info.revision.record_id)
    assert record.scope.project_id == "p1"
    stored = evidence.revision_bytes(revision_id)
    assert stored is not None and len(stored) == first["artifacts"][0]["byte_length"]
    (derivation,) = evidence.derivations_producing(revision_id)
    assert derivation.run_id == first["run_id"]
    assert derivation.module_id == "example.news"
    assert derivation.parameters_digest == receipt["derivation"]["parameters_digest"]


def test_reruns_reuse_the_revision_and_restarts_replay(
    real_host: Any, mod_env: Any, evidence: EvidenceStore
) -> None:
    first = _fetch(real_host, mod_env)
    second = _fetch(real_host, mod_env)
    assert (second["ingestion"]["created"], second["ingestion"]["reused"]) == (0, 1)
    artifact_id = first["artifacts"][0]["artifact_id"]
    assert (
        first["ingestion"]["artifact_revisions"][artifact_id]
        == second["ingestion"]["artifact_revisions"][artifact_id]
    )
    record_id = evidence.get_revision(
        first["ingestion"]["artifact_revisions"][artifact_id]
    ).revision.record_id  # type: ignore[union-attr]
    assert len(evidence.list_revisions("module:example.news--research", record_id)) == 1


def test_lineage_links_a_derived_run_to_its_parent_revision(
    real_host: Any, mod_env: Any, evidence: EvidenceStore
) -> None:
    parent = _fetch(real_host, mod_env)
    parent_artifact = parent["artifacts"][0]["artifact_id"]
    parent_revision = parent["ingestion"]["artifact_revisions"][parent_artifact]
    adapter = real_host.ingestion
    known = adapter.revisions_for([parent_artifact, "art_" + "0" * 32])
    assert known == {parent_artifact: parent_revision}
    job = real_host.invoke(
        "example.transcribe",
        "transcribe",
        {"artifact_id": parent_artifact},
        scope=SCOPE,
        ingest=True,
        wait=True,
    )
    assert job["state"] == "succeeded", job
    child = job["result"]["ingestion"]
    assert child["derivation"]["parent_revision_ids"] == [parent_revision]
    (child_revision,) = child["artifact_revisions"].values()
    (derivation,) = evidence.derivations_producing(child_revision)
    assert derivation.parent_revision_ids == (parent_revision,)
    assert derivation.module_id == "example.transcribe"


def test_default_workspace_uses_the_plain_source_id(real_host: Any, mod_env: Any) -> None:
    result = _fetch(real_host, mod_env, scope={"workspace_id": "default"})
    assert result["ingestion"]["source_id"] == "module:example.news"


def test_unknown_or_mismatched_destinations_are_rejected(
    real_host: Any, mod_env: Any, knowledge: KnowledgeStore
) -> None:
    with pytest.raises(UnknownScopeError):
        _fetch(real_host, mod_env, scope={"workspace_id": "nowhere"})
    with pytest.raises(UnknownScopeError):
        _fetch(real_host, mod_env, scope={"workspace_id": "other", "project_id": "p1"})
    other_sources = knowledge.list_sources("other")
    assert not any(s.kind == "module" for s in other_sources)


def test_a_record_is_never_moved_to_another_project(
    real_host: Any, mod_env: Any, knowledge: KnowledgeStore
) -> None:
    first = _fetch(real_host, mod_env)
    with pytest.raises(InvalidInputError, match="not moved"):
        _fetch(real_host, mod_env, scope={"workspace_id": "research", "project_id": "p2"})
    revision = first["ingestion"]["artifact_revisions"][first["artifacts"][0]["artifact_id"]]
    record_id = real_host.ingestion.evidence.get_revision(revision).revision.record_id
    record = knowledge.get_record("module:example.news--research", record_id)
    assert record.scope.project_id == "p1"


def test_source_ids_and_keys_fit_their_patterns() -> None:
    assert module_source_id_in("example.news", "default", "default") == "module:example.news"
    assert module_source_id_in("example.news", "lab", "default") == "module:example.news--lab"
    long_ws = "w" * 64
    long_id = module_source_id_in("m" * 64, long_ws, "default")
    assert long_id.startswith("module:" + "m" * 64 + "--w") and len(long_id) <= 7 + 96
    assert module_source_id_in("m" * 64, long_ws, "default") == long_id  # deterministic
    assert idempotency_key("art_1", "run_1") == "module:art_1:run_1"
    hashed = idempotency_key("art_1", "r" * 300)
    assert len(hashed) <= 200 and hashed.startswith("module:art_1:h")


def test_lazy_ingestion_creates_no_database_until_used(
    mod_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "lazy" / "knowledge.db"
    db.parent.mkdir()
    monkeypatch.setenv("KE_KNOWLEDGE_DB", str(db))
    lazy = ingestion_from_settings(Settings())
    assert isinstance(lazy, LazyEvidenceIngestion)
    before = lazy.revisions_for(["art_x"])
    assert before == {}
    assert not db.exists()
    with pytest.raises(UnknownScopeError):  # opens the store, then rejects the workspace
        lazy.ingest(
            [_payload()],
            destination=_scope("research"),
            derivation=_derivation(),
        )
    assert db.exists()
    lazy.close()
    lazy.close()  # idempotent


def test_disabled_knowledge_gives_unavailable_ingestion(
    mod_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KE_KNOWLEDGE_ENABLED", "0")
    disabled = ingestion_from_settings(Settings())
    assert isinstance(disabled, UnconfiguredIngestion)
    with pytest.raises(UnavailableError):
        disabled.ingest([_payload()], destination=_scope("default"), derivation=_derivation())


def test_shared_store_is_used_by_the_app_wiring(knowledge: KnowledgeStore, mod_env: Any) -> None:
    shared = ingestion_from_settings(Settings(), store=knowledge)
    assert isinstance(shared, EvidenceStoreIngestion)
    assert shared.evidence.store is knowledge


def test_host_close_closes_an_owned_store(make_host: Callable[..., Any], tmp_path: Path) -> None:
    db = tmp_path / "owned" / "knowledge.db"
    db.parent.mkdir()
    with KnowledgeStore.open(db) as setup:
        setup.ensure_workspace("default", "Default")
    lazy = LazyEvidenceIngestion(db, _policy(), default_workspace="default")
    host = make_host(ingestion=lazy)
    lazy._open()
    store = lazy._inner.evidence.store  # type: ignore[union-attr]
    host.close(drain_s=0.1)
    assert store.closed


# ── helpers ──────────────────────────────────────────────────────


def _policy() -> Any:
    from knowledge_engine.knowledge.evidence.policy import EvidencePolicy

    return EvidencePolicy()


def _scope(workspace_id: str) -> Any:
    from knowledge_engine.knowledge.contracts import RecordScope

    return RecordScope(workspace_id=workspace_id)


def _derivation() -> Any:
    from knowledge_engine.modules.ingestion import DerivationInput

    return DerivationInput(
        run_id="run_test",
        module_id="example.news",
        module_version="1.0.0",
        parameters_digest="sha256:" + "0" * 64,
    )


def _payload() -> Any:
    import hashlib
    from datetime import UTC, datetime

    from knowledge_engine.modules.artifacts import ArtifactPayload, NormalizedArtifact

    content = b"hello"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return ArtifactPayload(
        artifact=NormalizedArtifact(
            artifact_id="art_" + "1" * 32,
            module_id="example.news",
            module_version="1.0.0",
            run_id="run_test",
            scope=_scope("research"),
            media_type="text/plain",
            digest=digest,
            byte_length=len(content),
            canonical_uri="https://example.org/hello",
            created_at=datetime.now(UTC),
        ),
        content=content,
    )
