"""Evidence store: corpus snapshot, revisions, rename/edit/delete, ingestion API, resolution."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.core.config import Config
from knowledge_engine.core.registry import Registry
from knowledge_engine.knowledge import backfill
from knowledge_engine.knowledge.backfill import CORPUS_SOURCE_ID, corpus_record_id
from knowledge_engine.knowledge.contracts import (
    QueryContext,
    RetentionMode,
    RightsInfo,
    TextLocator,
    VerificationState,
    derive_revision_id,
    sha256_digest,
)
from knowledge_engine.knowledge.errors import (
    DeniedError,
    InvalidInputError,
    UnknownScopeError,
)
from knowledge_engine.knowledge.evidence.policy import EvidencePolicy
from knowledge_engine.knowledge.evidence.resolve import open_evidence, open_span
from knowledge_engine.knowledge.evidence.snapshot import snapshot
from knowledge_engine.knowledge.evidence.store import EvidenceStore, IngestRequest
from knowledge_engine.knowledge.store import MIGRATIONS_DIR, KnowledgeStore
from knowledge_engine.retrieval.fts5 import Indexer

GUIDE = "# Guide\n\nIntro text.\n\n## Usage\n\nRun the tool with care.\n"
FILES = {
    "libs/a/guide.md": GUIDE,
    "libs/a/notes.txt": "plain notes\n",
    "libs/a/dup-one.md": "# Same\n\nidentical bytes\n",
    "libs/a/dup-two.md": "# Same\n\nidentical bytes\n",
}
CTX = QueryContext(workspace_id="default")


@pytest.fixture
def corpus(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "corpus"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    registry = {
        "version": 1,
        "libraries": [{"id": "lib-a", "kind": "library", "name": "A", "path": "libs/a"}],
        "skills": [],
        "kits": [],
        "tools": [],
    }
    (root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    data = tmp_path / "data"
    backfill.apply(
        corpus_root=root,
        registry_path=root / "registry.json",
        db_path=data / "knowledge.db",
        workspace_id="default",
    )
    return {"root": root, "registry": root / "registry.json", "db": data / "knowledge.db"}


def _evidence(corpus: dict[str, Path], **policy: Any) -> Iterator[EvidenceStore]:
    store = KnowledgeStore.open(corpus["db"])
    try:
        yield EvidenceStore(store, EvidencePolicy(**policy))
    finally:
        store.close()


@pytest.fixture
def evidence(corpus: dict[str, Path]) -> Iterator[EvidenceStore]:
    yield from _evidence(corpus)


def _snap(ev: EvidenceStore, corpus: dict[str, Path], **kw: Any) -> dict[str, Any]:
    return snapshot(
        ev,
        corpus_root=corpus["root"],
        registry_path=corpus["registry"],
        apply=kw.pop("apply", True),
        **kw,
    )


def _usage_span(ev: EvidenceStore, record_id: str) -> tuple[str, str]:
    state = ev.record_state(CORPUS_SOURCE_ID, record_id)
    assert state.current_revision_id is not None
    spans = ev.list_spans(state.current_revision_id)
    usage = [
        s for s in spans if isinstance(s.locator, TextLocator) and s.locator.heading == "Usage"
    ]
    assert len(usage) == 1
    return usage[0].evidence_id, state.current_revision_id


GUIDE_ID = corpus_record_id("libs/a/guide.md")


# ── Snapshot ─────────────────────────────────────────────────────────


def test_snapshot_dry_run_writes_nothing(corpus: dict[str, Path], evidence: EvidenceStore) -> None:
    report = _snap(evidence, corpus, apply=False)
    assert report["mode"] == "dry-run"
    assert report["first_revisions"] == 4 and report["new_records"] == 0
    assert evidence.stats()["revisions"] == 0


def test_snapshot_records_revisions_idempotently(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    first = _snap(evidence, corpus)
    assert first["revisions_created"] == 4 and first["failure_count"] == 0
    stats = evidence.stats()
    assert stats["revisions"] == 4 and stats["retained_revisions"] == 4
    assert stats["outbox_pending"] == 4
    state = evidence.record_state(CORPUS_SOURCE_ID, GUIDE_ID)
    data = GUIDE.encode()
    assert state.current_revision_id == derive_revision_id(
        CORPUS_SOURCE_ID, GUIDE_ID, sha256_digest(data)
    )
    again = _snap(evidence, corpus)
    assert again["revisions_created"] == 0 and again["unchanged"] == 4
    assert evidence.stats()["revisions"] == 4
    assert evidence.record_state(CORPUS_SOURCE_ID, GUIDE_ID).current_revision_id == (
        state.current_revision_id
    )


def test_identical_bytes_in_two_records_stay_two_revisions(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    one = evidence.record_state(CORPUS_SOURCE_ID, corpus_record_id("libs/a/dup-one.md"))
    two = evidence.record_state(CORPUS_SOURCE_ID, corpus_record_id("libs/a/dup-two.md"))
    assert one.current_revision_id != two.current_revision_id
    a = evidence.get_revision(str(one.current_revision_id))
    b = evidence.get_revision(str(two.current_revision_id))
    assert a is not None and b is not None
    assert a.revision.digest == b.revision.digest


def test_citation_resolves_after_reindex(corpus: dict[str, Path], evidence: EvidenceStore) -> None:
    _snap(evidence, corpus)
    evidence_id, revision_id = _usage_span(evidence, GUIDE_ID)
    config = Config(
        corpus_root=corpus["root"], data_dir=corpus["db"].parent, registry_path=corpus["registry"]
    )
    registry = Registry(corpus["registry"])
    indexer = Indexer(config, registry)
    try:
        indexer.rebuild()
        indexer.rebuild()
    finally:
        indexer.close()
        registry.close()
    _snap(evidence, corpus)  # re-recording after a reindex changes no identity
    res = open_span(evidence, CTX, evidence_id)
    assert res.verification == VerificationState.VERIFIED
    assert res.result.revision_id == revision_id
    assert res.result.content == "## Usage\n\nRun the tool with care.\n"
    assert res.evidence_id == evidence_id


def test_edit_makes_the_old_citation_stale_and_still_verifiable(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    evidence_id, old_revision = _usage_span(evidence, GUIDE_ID)
    (corpus["root"] / "libs/a/guide.md").write_text(GUIDE + "\nMore.\n", encoding="utf-8")
    report = _snap(evidence, corpus)
    assert report["edited"] == 1 and report["revisions_created"] == 1
    res = open_span(evidence, CTX, evidence_id)
    assert res.verification == VerificationState.STALE
    assert res.result.revision_id == old_revision  # never follows to the latest revision
    assert res.result.content == "## Usage\n\nRun the tool with care.\n"
    assert res.current_revision_id != old_revision
    revisions = evidence.list_revisions(CORPUS_SOURCE_ID, GUIDE_ID)
    assert revisions[0].revision.revision_id == old_revision
    assert len(revisions) == 2


def test_deletion_leaves_a_tombstone_and_citations_unavailable(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    evidence_id, revision_id = _usage_span(evidence, GUIDE_ID)
    (corpus["root"] / "libs/a/guide.md").unlink()
    dry = _snap(evidence, corpus)  # without --delete-missing nothing is deleted
    assert dry["missing"] == 1 and dry["deleted"] == 0
    assert open_span(evidence, CTX, evidence_id).verification == VerificationState.VERIFIED
    report = _snap(evidence, corpus, delete_missing=True)
    assert report["deleted"] == 1
    res = open_span(evidence, CTX, evidence_id)
    assert res.verification == VerificationState.UNAVAILABLE
    assert res.result.content is None and res.reason and "deleted" in res.reason
    tomb = evidence.tombstone(revision_id)
    assert tomb is not None and tomb.reason == "file removed from the corpus"
    assert evidence.revision_bytes(revision_id) is None
    assert evidence.stats()["tombstones"] == 1


def test_purge_policy_removes_every_trace(corpus: dict[str, Path]) -> None:
    for ev in _evidence(corpus, on_delete="purge"):
        _snap(ev, corpus)
        evidence_id, revision_id = _usage_span(ev, GUIDE_ID)
        result = ev.delete_record(CORPUS_SOURCE_ID, GUIDE_ID, reason="rights request")
        assert result["mode"] == "purge" and result["tombstones"] == 0
        assert ev.get_revision(revision_id) is None and ev.tombstone(revision_id) is None
        with pytest.raises(UnknownScopeError):
            open_span(ev, CTX, evidence_id)


def test_deletion_is_skipped_while_an_entry_has_no_content(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    registry = json.loads(corpus["registry"].read_text(encoding="utf-8"))
    registry["libraries"].append({"id": "gone", "kind": "library", "name": "G", "path": "nope"})
    corpus["registry"].write_text(json.dumps(registry), encoding="utf-8")
    (corpus["root"] / "libs/a/notes.txt").unlink()
    report = _snap(evidence, corpus, delete_missing=True)
    assert report["deletion_blocked_by_missing_entries"] == ["gone"]
    assert report["deleted"] == 0


def test_rename_keeps_identity_through_an_alias(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    evidence_id, revision_id = _usage_span(evidence, GUIDE_ID)
    root = corpus["root"]
    (root / "libs/a/guide.md").rename(root / "libs/a/handbook.md")
    report = _snap(evidence, corpus)
    assert report["renamed"] == [
        {"from": "libs/a/guide.md", "to": "libs/a/handbook.md", "record_id": GUIDE_ID}
    ]
    assert report["revisions_created"] == 0 and report["new_records"] == 0
    record = evidence.record_state(CORPUS_SOURCE_ID, GUIDE_ID).record
    assert record.logical_path == "libs/a/handbook.md"
    assert record.aliases == ("libs/a/guide.md",)
    assert evidence.find_records(CORPUS_SOURCE_ID, "libs/a/guide.md") == [GUIDE_ID]
    res = open_span(evidence, CTX, evidence_id)
    assert res.verification == VerificationState.VERIFIED and res.result.revision_id == revision_id
    # A new file at the old path is a different record, not the renamed one.
    (root / "libs/a/guide.md").write_text("# New guide\n", encoding="utf-8")
    later = _snap(evidence, corpus)
    assert later["new_records"] == 1
    matches = evidence.find_records(CORPUS_SOURCE_ID, "libs/a/guide.md")
    assert len(matches) == 2 and GUIDE_ID in matches  # path + alias: ambiguous by path


def test_ambiguous_renames_are_not_guessed(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    root = corpus["root"]
    (root / "libs/a/dup-one.md").rename(root / "libs/a/dup-three.md")
    (root / "libs/a/dup-two.md").rename(root / "libs/a/dup-four.md")
    report = _snap(evidence, corpus)
    assert report["renamed_count"] == 0
    assert report["new_records"] == 2 and report["missing"] == 2


def test_upgrade_from_schema_1_keeps_records(tmp_path: Path) -> None:
    from knowledge_engine.core.util.sqlite import apply_migrations, connect, discover_migrations

    db = tmp_path / "k.db"
    conn = connect(db)
    v1 = [m for m in discover_migrations(MIGRATIONS_DIR) if m.version == 1]
    apply_migrations(conn, v1)
    conn.execute(
        "INSERT INTO workspaces VALUES ('default', 'Default', '', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO sources VALUES ('corpus:base', 'default', 'corpus', 'Base', '{}',"
        " '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO records (source_id, record_id, logical_path, title, created_at, updated_at)"
        " VALUES ('corpus:base', 'doc-1', 'a.md', 'A', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()
    with KnowledgeStore.open(db) as store:
        assert store.schema_version() == 2
        record = store.get_record("corpus:base", "doc-1")
        assert record.rights.retention == RetentionMode.RETAIN
        assert record.aliases == ()
    with KnowledgeStore.open(db) as store:  # idempotent rerun
        assert store.schema_version() == 2


# ── Ingestion API ────────────────────────────────────────────────────


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[EvidenceStore]:
    store = KnowledgeStore.open(tmp_path / "lab.db")
    store.create_workspace("Lab", workspace_id="lab")
    store.register_source("lab", "module:papers", kind="module", display_name="Papers")
    try:
        yield EvidenceStore(store, EvidencePolicy(max_bytes=1000))
    finally:
        store.close()


def _req(**kw: Any) -> IngestRequest:
    base: dict[str, Any] = {
        "source_id": "module:papers",
        "record_id": "paper-1",
        "logical_path": "papers/one.md",
        "media_type": "text/markdown",
        "content": b"# One\n\nbody\n",
    }
    base.update(kw)
    return IngestRequest.model_validate(base)


def test_ingest_is_idempotent_by_key_and_inputs(lab: EvidenceStore) -> None:
    first = lab.ingest(_req(idempotency_key="k1"))
    assert first.revision_created and first.record_created and not first.replayed
    again = lab.ingest(_req(idempotency_key="k1"))
    assert again.replayed and not again.revision_created
    assert again.revision.revision_id == first.revision.revision_id
    with pytest.raises(InvalidInputError) as exc:
        lab.ingest(_req(idempotency_key="k1", content=b"# Other\n"))
    assert exc.value.details["reason"] == "idempotency_conflict"
    no_key = lab.ingest(_req())
    assert not no_key.revision_created and not no_key.head_changed
    assert lab.stats()["revisions"] == 1


def test_ingest_failure_rolls_back_atomically(lab: EvidenceStore) -> None:
    bad_span = {"locator": {"kind": "text", "start_byte": 0, "end_byte": 999}}
    with pytest.raises(InvalidInputError) as exc:
        lab.ingest(_req(spans=[{**bad_span, "extraction": {"method": "producer", "version": "1"}}]))
    assert exc.value.details["reason"] == "span_invalid"
    assert lab.stats()["revisions"] == 0
    with pytest.raises(UnknownScopeError):
        lab.record_state("module:papers", "paper-1")


def test_ingest_rejects_bad_inputs(lab: EvidenceStore) -> None:
    with pytest.raises(InvalidInputError) as exc:
        lab.ingest(_req(content=b"x" * 1001))
    assert exc.value.details["reason"] == "too_large"
    with pytest.raises(UnknownScopeError):
        lab.ingest(_req(source_id="module:missing"))
    with pytest.raises(InvalidInputError):
        lab.ingest({"source_id": "module:papers", "record_id": "x", "media_type": "nope"})
    with pytest.raises(InvalidInputError):
        lab.ingest({**_req().model_dump(), "logical_path": "../escape.md"})
    lab.ingest(_req())
    with pytest.raises(InvalidInputError) as conflict:
        lab.ingest(_req(record_id="paper-2"))
    assert conflict.value.details["reason"] == "path_conflict"


def test_digest_only_retention_keeps_no_bytes(lab: EvidenceStore) -> None:
    with pytest.raises(InvalidInputError) as exc:
        lab.ingest(_req(rights=RightsInfo(retention=RetentionMode.DIGEST_ONLY)))
    assert exc.value.details["reason"] == "external_locator_required"
    result = lab.ingest(
        _req(
            rights=RightsInfo(retention=RetentionMode.DIGEST_ONLY, license="proprietary"),
            canonical_uri="https://example.org/one",
        )
    )
    revision = result.revision
    assert not revision.bytes_retained and revision.unavailable_reason
    assert lab.revision_bytes(revision.revision_id) is None
    res = open_evidence(
        lab,
        QueryContext(workspace_id="lab"),
        source_id="module:papers",
        record_id="paper-1",
        revision_id=revision.revision_id,
    )
    assert res.verification == VerificationState.UNAVAILABLE and res.result.content is None
    assert res.result.digest == sha256_digest(b"# One\n\nbody\n")


def test_tombstone_retention_refuses_new_bytes(lab: EvidenceStore) -> None:
    from knowledge_engine.knowledge.errors import DeniedError as Denied

    with pytest.raises(Denied):
        lab.ingest(_req(rights=RightsInfo(retention=RetentionMode.TOMBSTONE)))


def test_derivation_lineage_and_outbox(lab: EvidenceStore) -> None:
    parent = lab.ingest(_req()).revision.revision_id
    derivation = {
        "run_id": "run-1",
        "module_id": "transcriber",
        "module_version": "1.0",
        "parameters_digest": sha256_digest(b"{}"),
        "parent_revision_ids": [parent],
    }
    child = lab.ingest(
        _req(record_id="t-1", logical_path="t/one.md", content=b"# T\n", derivation=derivation)
    )
    lineage = lab.derivations_producing(child.revision.revision_id)
    assert [d.parent_revision_ids for d in lineage] == [(parent,)]
    res = open_evidence(
        lab,
        QueryContext(workspace_id="lab"),
        source_id="module:papers",
        record_id="t-1",
        revision_id=child.revision.revision_id,
    )
    assert res.result.provenance_id == "run-1"
    with pytest.raises(InvalidInputError) as exc:
        lab.ingest(
            _req(
                record_id="t-2",
                logical_path="t/two.md",
                content=b"# T2\n",
                derivation={**derivation, "module_version": "2.0"},
            )
        )
    assert exc.value.details["reason"] == "derivation_conflict"
    pending = lab.pending_outbox()
    assert [e["event"] for e in pending] == ["revision_added", "revision_added"]
    assert lab.ack_outbox([e["seq"] for e in pending]) == 2
    assert lab.ack_outbox([e["seq"] for e in pending]) == 0
    assert lab.pending_outbox() == []


def test_reingest_after_tombstone_restores(lab: EvidenceStore) -> None:
    first = lab.ingest(_req()).revision.revision_id
    lab.delete_record("module:papers", "paper-1", reason="test")
    again = lab.ingest(_req())
    assert again.restored and again.revision.revision_id == first
    assert lab.tombstone(first) is None
    assert lab.revision_bytes(first) == b"# One\n\nbody\n"


def test_api_rename_records_an_alias(lab: EvidenceStore) -> None:
    lab.ingest(_req())
    record = lab.rename("module:papers", "paper-1", "papers/renamed.md")
    assert record.logical_path == "papers/renamed.md" and record.aliases == ("papers/one.md",)
    assert lab.ingest(_req(logical_path="papers/renamed.md")).renamed_from is None
    with pytest.raises(InvalidInputError):
        lab.rename("module:papers", "paper-1", "/abs")


# ── Resolution ───────────────────────────────────────────────────────


def test_open_rechecks_scope_and_exact_identity(
    corpus: dict[str, Path], evidence: EvidenceStore
) -> None:
    _snap(evidence, corpus)
    _, revision_id = _usage_span(evidence, GUIDE_ID)
    project = evidence.store.create_project("default", "Other", project_id="other")
    with pytest.raises(DeniedError):
        open_evidence(
            evidence,
            QueryContext(workspace_id="default", project_id=project.project_id),
            source_id=CORPUS_SOURCE_ID,
            record_id=GUIDE_ID,
            revision_id=revision_id,
        )
    other = corpus_record_id("libs/a/notes.txt")
    with pytest.raises(UnknownScopeError):  # a revision of another record is not followed
        open_evidence(
            evidence, CTX, source_id=CORPUS_SOURCE_ID, record_id=other, revision_id=revision_id
        )
    with pytest.raises(InvalidInputError) as exc:
        open_evidence(
            evidence,
            QueryContext(workspace_id="default", byte_budget=4),
            source_id=CORPUS_SOURCE_ID,
            record_id=GUIDE_ID,
            revision_id=revision_id,
        )
    assert exc.value.details["reason"] == "byte_budget"
    whole = open_evidence(
        evidence, CTX, source_id=CORPUS_SOURCE_ID, record_id=GUIDE_ID, revision_id=revision_id
    )
    assert whole.verification == VerificationState.VERIFIED and whole.result.content == GUIDE


def test_corrupted_bytes_are_unavailable(corpus: dict[str, Path], evidence: EvidenceStore) -> None:
    _snap(evidence, corpus)
    evidence_id, revision_id = _usage_span(evidence, GUIDE_ID)
    conn = sqlite3.connect(corpus["db"])
    conn.execute("UPDATE revision_bytes SET content = ? WHERE revision_id = ?", (b"x", revision_id))
    conn.commit()
    conn.close()
    res = open_span(evidence, CTX, evidence_id)
    assert res.verification == VerificationState.UNAVAILABLE
    assert res.reason is not None and "digest" in res.reason


def test_snapshot_requires_the_migrated_source(tmp_path: Path) -> None:
    with KnowledgeStore.open(tmp_path / "empty.db") as store:
        with pytest.raises(UnknownScopeError) as exc:
            snapshot(
                EvidenceStore(store),
                corpus_root=tmp_path,
                registry_path=tmp_path / "registry.json",
                apply=True,
            )
        assert "migrate" in exc.value.details["hint"]


def test_large_files_are_recorded_digest_only(corpus: dict[str, Path]) -> None:
    for ev in _evidence(corpus, max_bytes=30):
        report = _snap(ev, corpus)
        assert report["digest_only"] == 1  # only guide.md exceeds 30 bytes
        state = ev.record_state(CORPUS_SOURCE_ID, GUIDE_ID)
        info = ev.get_revision(str(state.current_revision_id))
        assert info is not None and not info.revision.bytes_retained
        assert info.external_locator == f"{CORPUS_SOURCE_ID}/libs/a/guide.md"
        assert info.revision.digest == sha256_digest(GUIDE.encode())


def test_policy_validation() -> None:
    with pytest.raises(InvalidInputError):
        EvidencePolicy(on_delete="shred")  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        EvidencePolicy(default_retention=RetentionMode.TOMBSTONE)
    with pytest.raises(InvalidInputError):
        EvidencePolicy(max_bytes=0)
