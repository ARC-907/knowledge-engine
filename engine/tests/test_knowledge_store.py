"""The scoped-knowledge metadata store: identity, membership, classification, failures."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from knowledge_engine.core.util.sqlite import connect
from knowledge_engine.knowledge.contracts import CurationStatus, ResearchType
from knowledge_engine.knowledge.errors import (
    IncompatibleError,
    InvalidInputError,
    InvalidScopeError,
    UnavailableError,
    UnknownScopeError,
)
from knowledge_engine.knowledge.store import KnowledgeStore, NewRecord, latest_schema_version


@pytest.fixture
def store(tmp_path: Path) -> Iterator[KnowledgeStore]:
    with KnowledgeStore.open(tmp_path / "knowledge.db") as s:
        s.create_workspace("Lab", workspace_id="lab")
        s.register_source("lab", "corpus:base", kind="corpus", display_name="Base corpus")
        s.register_records("corpus:base", [NewRecord("doc-1", "a/doc1.md", "Doc 1")])
        yield s


def test_migration_creates_versioned_schema_without_default_rows(tmp_path: Path) -> None:
    with KnowledgeStore.open(tmp_path / "k.db") as s:
        assert s.schema_version() == latest_schema_version() == 1
        # No workspace exists until the backfill or an admin call creates one.
        assert s.list_workspaces() == []
        assert s.status()["counts"]["workspaces"] == 0


def test_two_projects_with_the_same_display_name_are_distinct(store: KnowledgeStore) -> None:
    first = store.create_project("lab", "Thesis")
    second = store.create_project("lab", "Thesis")
    assert first.project_id != second.project_id
    assert first.project_id.startswith("prj-")
    listed = store.list_projects("lab")
    assert [p.display_name for p in listed] == ["Thesis", "Thesis"]
    store.classify("corpus:base", "doc-1", project_id=first.project_id)
    assert store.get_record("corpus:base", "doc-1").scope.project_id == first.project_id
    with pytest.raises(InvalidInputError, match="already exists"):
        store.create_project("lab", "Other", project_id=first.project_id)


def test_workspace_display_name_may_change_identity_does_not(store: KnowledgeStore) -> None:
    renamed = store.rename_workspace("lab", "Research lab")
    assert renamed.workspace_id == "lab" and renamed.display_name == "Research lab"
    workspace, created = store.ensure_workspace("lab", "ignored")
    assert not created and workspace.display_name == "Research lab"
    with pytest.raises(InvalidInputError):
        store.create_workspace("Dup", workspace_id="lab")


def test_multi_collection_membership(store: KnowledgeStore) -> None:
    a = store.create_collection("lab", "Methods")
    b = store.create_collection("lab", "Reading list")
    assert store.add_members(a.collection_id, [("corpus:base", "doc-1")]) == {
        "added": 1,
        "unchanged": 0,
    }
    store.add_members(b.collection_id, [{"source_id": "corpus:base", "record_id": "doc-1"}])
    # Adding again is idempotent.
    assert store.add_members(a.collection_id, [("corpus:base", "doc-1")])["unchanged"] == 1
    record = store.get_record("corpus:base", "doc-1")
    assert record.collection_ids == tuple(sorted((a.collection_id, b.collection_id)))
    assert store.get_collection(a.collection_id).member_count == 1
    assert store.remove_members(a.collection_id, [("corpus:base", "doc-1")])["removed"] == 1
    assert store.get_record("corpus:base", "doc-1").collection_ids == (b.collection_id,)


def test_legacy_record_defaults_are_unclassified(store: KnowledgeStore) -> None:
    record = store.get_record("corpus:base", "doc-1")
    assert record.research_type is ResearchType.OTHER
    assert record.curation_status is CurationStatus.UNCLASSIFIED
    assert record.scope.project_id is None and record.collection_ids == () and record.tags == ()
    # Registering it again never overwrites curation.
    store.classify("corpus:base", "doc-1", research_type="paper", curation_status="reviewed")
    assert store.register_records("corpus:base", [NewRecord("doc-1", "a/doc1.md")]) == (0, 1)
    assert store.get_record("corpus:base", "doc-1").research_type is ResearchType.PAPER


def test_classification_edits(store: KnowledgeStore) -> None:
    store.classify("corpus:base", "doc-1", tags=["B", "a"])
    assert store.get_record("corpus:base", "doc-1").tags == ("a", "b")
    store.classify("corpus:base", "doc-1", add_tags=["c"], remove_tags=["a"])
    assert store.get_record("corpus:base", "doc-1").tags == ("b", "c")
    with pytest.raises(InvalidInputError):
        store.classify("corpus:base", "doc-1", tags=["x"], add_tags=["y"])
    with pytest.raises(InvalidInputError):
        store.classify("corpus:base", "doc-1", research_type="podcast")
    with pytest.raises(InvalidInputError):
        store.classify("corpus:base", "doc-1", curation_status="approved")
    with pytest.raises(InvalidInputError):
        store.classify("corpus:base", "doc-1", add_tags=["bad tag!"])
    with pytest.raises(UnknownScopeError):
        store.classify("corpus:base", "doc-404", research_type="note")


def test_project_collection_constraints(store: KnowledgeStore) -> None:
    alpha = store.create_project("lab", "Alpha")
    beta = store.create_project("lab", "Beta")
    col = store.create_collection("lab", "Alpha only", project_id=alpha.project_id)
    # Workspace-wide content may be curated into a project collection without moving it.
    store.add_members(col.collection_id, [("corpus:base", "doc-1")])
    assert store.get_record("corpus:base", "doc-1").scope.project_id is None
    # ...but it cannot then be moved into another project.
    with pytest.raises(InvalidScopeError):
        store.classify("corpus:base", "doc-1", project_id=beta.project_id)
    store.classify("corpus:base", "doc-1", project_id=alpha.project_id)
    store.classify("corpus:base", "doc-1", project_id=None)
    assert store.get_record("corpus:base", "doc-1").scope.project_id is None
    store.remove_members(col.collection_id, [("corpus:base", "doc-1")])
    store.classify("corpus:base", "doc-1", project_id=beta.project_id)
    with pytest.raises(InvalidScopeError):
        store.add_members(col.collection_id, [("corpus:base", "doc-1")])


def test_cross_workspace_references_are_rejected(store: KnowledgeStore) -> None:
    store.create_workspace("Other", workspace_id="other")
    foreign = store.create_project("other", "Elsewhere")
    with pytest.raises(InvalidScopeError):
        store.create_collection("lab", "Bad", project_id=foreign.project_id)
    with pytest.raises(InvalidScopeError):
        store.classify("corpus:base", "doc-1", project_id=foreign.project_id)
    other_col = store.create_collection("other", "Theirs")
    with pytest.raises(InvalidScopeError):
        store.add_members(other_col.collection_id, [("corpus:base", "doc-1")])
    with pytest.raises(InvalidInputError, match="already registered"):
        store.register_source("other", "corpus:base", kind="corpus", display_name="Again")


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda s: s.get_workspace("nope"), UnknownScopeError),
        (lambda s: s.list_projects("nope"), UnknownScopeError),
        (lambda s: s.get_source("corpus:nope"), UnknownScopeError),
        (lambda s: s.get_collection("nope"), UnknownScopeError),
        (lambda s: s.create_workspace("X", workspace_id="Bad Id"), InvalidInputError),
        (lambda s: s.create_project("lab", "   "), InvalidInputError),
        (
            lambda s: s.register_source("lab", "nonamespace", kind="corpus", display_name="x"),
            InvalidInputError,
        ),
        (
            lambda s: s.register_source("lab", "x:y", kind="weird", display_name="x"),
            InvalidInputError,
        ),
        (lambda s: s.add_members("nope", []), InvalidInputError),
        (lambda s: s.add_members("nope", [("corpus:base", "doc-1")]), UnknownScopeError),
        (lambda s: s.add_members("nope", ["garbage"]), InvalidInputError),
    ],
)
def test_validation_failures_are_typed(store: KnowledgeStore, call, error) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(error):
        call(store)


def test_store_refuses_a_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "k.db"
    KnowledgeStore.open(path).close()
    conn = connect(path)
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'later')")
    conn.commit()
    conn.close()
    with pytest.raises(IncompatibleError) as exc:
        KnowledgeStore.open(path)
    assert exc.value.details["schema_version"] == 99
    with pytest.raises(IncompatibleError):
        KnowledgeStore.open_readonly(path)


def test_readonly_store_never_writes(tmp_path: Path) -> None:
    with pytest.raises(UnavailableError):
        KnowledgeStore.open_readonly(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()
    path = tmp_path / "k.db"
    with KnowledgeStore.open(path) as s:
        s.create_workspace("Lab", workspace_id="lab")
    with KnowledgeStore.open_readonly(path) as ro:
        assert [w.workspace_id for w in ro.list_workspaces()] == ["lab"]
        with pytest.raises(InvalidInputError, match="read-only"):
            ro.create_workspace("X")


def test_closed_store_is_unavailable(tmp_path: Path) -> None:
    s = KnowledgeStore.open(tmp_path / "k.db")
    s.close()
    s.close()  # idempotent
    with pytest.raises(UnavailableError):
        s.list_workspaces()


def test_database_errors_become_unavailable(
    store: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_insert_records", broken)
    with pytest.raises(UnavailableError):
        store.register_records("corpus:base", [NewRecord("doc-2", "b.md")])


def test_failed_write_rolls_back(store: KnowledgeStore) -> None:
    col = store.create_collection("lab", "C")
    with pytest.raises(UnknownScopeError):
        store.add_members(col.collection_id, [("corpus:base", "doc-1"), ("corpus:base", "nope")])
    assert store.get_record("corpus:base", "doc-1").collection_ids == ()


def test_store_is_safe_across_threads(store: KnowledgeStore) -> None:
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(10):
                store.create_project("lab", f"P{n}-{i}")
                store.list_projects("lab")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.list_projects("lab")) == 40
