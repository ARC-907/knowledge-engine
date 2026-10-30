"""Filter semantics of scoped-knowledge queries (ADR 0006), and store/predicate parity.

The fixture workspace has two projects with the same display name, records in
two sources, a multi-collection record and unclassified legacy content. Every
row of :data:`CASES` is checked against the store's SQL filter *and* the
reference predicate :func:`knowledge_engine.knowledge.scope.record_in_scope`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.knowledge.contracts import (
    QueryContext,
    ResearchType,
    ScopeProfile,
    TrustedPrincipal,
)
from knowledge_engine.knowledge.errors import (
    DeniedError,
    InvalidScopeError,
    KnowledgeError,
    UnknownScopeError,
)
from knowledge_engine.knowledge.scope import RecordView, record_in_scope
from knowledge_engine.knowledge.store import KnowledgeStore, NewRecord

ALL = {"r1", "r2", "r3", "r4", "r5"}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[KnowledgeStore]:
    with KnowledgeStore.open(tmp_path / "knowledge.db") as s:
        _populate(s)
        yield s


def _populate(s: KnowledgeStore) -> None:
    s.create_workspace("Lab", workspace_id="lab")
    s.create_workspace("Elsewhere", workspace_id="other")
    s.create_project("lab", "Thesis", project_id="alpha")
    s.create_project("lab", "Thesis", project_id="beta")  # same display name, distinct project
    s.register_source("lab", "corpus:base", kind="corpus", display_name="Base corpus")
    s.register_source("lab", "notes:local", kind="other", display_name="Notes")
    s.register_source("other", "corpus:other", kind="corpus", display_name="Other corpus")
    s.register_records("corpus:base", [NewRecord(r, f"{r}.md") for r in ("r1", "r2", "r3")])
    s.register_records("notes:local", [NewRecord(r, f"{r}.md") for r in ("r4", "r5")])
    s.register_records("corpus:other", [NewRecord("x1", "x1.md")])
    s.create_collection("lab", "ML", collection_id="c-ml")
    s.create_collection("lab", "Reading", collection_id="c-reading")
    s.create_collection("lab", "Alpha notes", collection_id="c-alpha", project_id="alpha")
    s.create_collection("lab", "Beta notes", collection_id="c-beta", project_id="beta")
    s.classify("corpus:base", "r1", research_type="paper", tags=["ML", "survey"])
    s.classify("corpus:base", "r2", research_type="note", tags=["ml"], project_id="alpha")
    s.classify("corpus:base", "r3", research_type="note", project_id="beta")
    s.classify(
        "notes:local", "r4", research_type="paper", tags=["ml", "Survey"], project_id="alpha"
    )
    # r5 stays as legacy content: unclassified, research type other, no project.
    s.add_members("c-ml", [("corpus:base", "r1"), ("notes:local", "r4")])
    s.add_members("c-reading", [("corpus:base", "r1")])
    s.add_members("c-alpha", [("corpus:base", "r2")])


CASES: list[tuple[str, dict[str, Any], set[str]]] = [
    ("omitted selections use the default profile", {}, ALL),
    ("explicit empty sources select nothing", {"source_ids": []}, set()),
    ("explicit empty collections select nothing", {"collection_ids": []}, set()),
    ("explicit empty types select nothing", {"research_types": []}, set()),
    ("one source", {"source_ids": ["notes:local"]}, {"r4", "r5"}),
    ("sources are ORed", {"source_ids": ["corpus:base", "notes:local"]}, ALL),
    ("one tag", {"tags": ["ml"]}, {"r1", "r2", "r4"}),
    ("tags are ANDed", {"tags": ["ml", "survey"]}, {"r1", "r4"}),
    ("explicit empty tags: no tag constraint", {"tags": []}, ALL),
    ("collections are ORed", {"collection_ids": ["c-ml", "c-reading"]}, {"r1", "r4"}),
    ("types are ORed", {"research_types": ["paper", "note"]}, {"r1", "r2", "r3", "r4"}),
    ("legacy content is type other", {"research_types": ["other"]}, {"r5"}),
    ("project only", {"project_id": "alpha"}, {"r2", "r4"}),
    ("same display name, other project", {"project_id": "beta"}, {"r3"}),
    (
        "project plus workspace-wide content",
        {"project_id": "alpha", "include_workspace": True},
        {"r1", "r2", "r4", "r5"},
    ),
    ("dimensions are ANDed", {"project_id": "alpha", "research_types": ["paper"]}, {"r4"}),
    ("source AND tag", {"source_ids": ["notes:local"], "tags": ["ml"]}, {"r4"}),
    ("project collection", {"project_id": "alpha", "collection_ids": ["c-alpha"]}, {"r2"}),
    (
        "workspace collection inside a project query",
        {"project_id": "alpha", "collection_ids": ["c-ml"]},
        {"r4"},
    ),
]


def _ids(records: list[Any]) -> set[str]:
    return {r.record_id for r in records}


def _views(store: KnowledgeStore) -> list[RecordView]:
    scope = store.resolve(QueryContext(workspace_id="lab"))
    records, _ = store.list_records(scope, limit=500)
    return [
        RecordView(
            source_id=r.source_id,
            record_id=r.record_id,
            project_id=r.scope.project_id,
            research_type=r.research_type,
            tags=frozenset(r.tags),
            collection_ids=frozenset(r.collection_ids),
        )
        for r in records
    ]


@pytest.mark.parametrize(("name", "selection", "expected"), CASES, ids=[c[0] for c in CASES])
def test_filter_semantics(
    store: KnowledgeStore, name: str, selection: dict[str, Any], expected: set[str]
) -> None:
    context = QueryContext(workspace_id="lab", **selection)
    scope, records, total = store.query_records(context)
    assert _ids(records) == expected, name
    assert total == len(expected)
    # Store SQL and the reference predicate agree.
    assert {v.record_id for v in _views(store) if record_in_scope(v, scope)} == expected
    # Workspace boundary: the other workspace's record never appears.
    assert "x1" not in _ids(records)
    # The broker's search snapshot (V2 P2) loads exactly the same records.
    snapshot = store.snapshot(context)
    assert {rid for recs in snapshot.records.values() for rid in recs} == expected
    for recs in snapshot.records.values():
        assert all(record_in_scope(rec.view, snapshot.scope) for rec in recs.values())


def test_effective_scope_reports_profile_and_emptiness(store: KnowledgeStore) -> None:
    scope = store.resolve(QueryContext(workspace_id="lab"))
    assert scope.source_ids == ("corpus:base", "notes:local")
    assert scope.profile_id == "default"
    assert set(scope.from_profile) == {"source_ids", "collection_ids", "research_types", "tags"}
    assert not scope.empty
    empty = store.resolve(QueryContext(workspace_id="lab", source_ids=[]))
    assert empty.empty and empty.source_ids == ()
    explicit = store.resolve(QueryContext(workspace_id="lab", tags=["ml"]))
    assert "tags" not in explicit.from_profile


@pytest.mark.parametrize(
    ("selection", "error", "kind"),
    [
        ({"source_ids": ["corpus:nope"]}, UnknownScopeError, "source"),
        ({"source_ids": ["corpus:other"]}, UnknownScopeError, "source"),  # other workspace
        ({"collection_ids": ["c-nope"]}, UnknownScopeError, "collection"),
        ({"project_id": "gamma"}, UnknownScopeError, "project"),
        ({"profile_id": "missing"}, UnknownScopeError, "profile"),
        ({"project_id": "alpha", "collection_ids": ["c-beta"]}, InvalidScopeError, None),
    ],
)
def test_bad_scope_fails_the_whole_request(
    store: KnowledgeStore, selection: dict[str, Any], error: type[KnowledgeError], kind: str | None
) -> None:
    with pytest.raises(error) as exc:
        store.query_records(QueryContext(workspace_id="lab", **selection))
    if kind is not None:
        assert exc.value.details["kind"] == kind


def test_unknown_workspace_is_rejected(store: KnowledgeStore) -> None:
    with pytest.raises(UnknownScopeError) as exc:
        store.query_records(QueryContext(workspace_id="nowhere"))
    assert exc.value.code == "unknown_scope"


def test_trusted_principal_limits_workspaces(store: KnowledgeStore) -> None:
    context = QueryContext(workspace_id="lab").model_copy(
        update={"principal": TrustedPrincipal(principal_id="svc", allowed_workspaces=("other",))}
    )
    with pytest.raises(DeniedError):
        store.resolve(context)
    allowed = context.model_copy(
        update={"principal": TrustedPrincipal(principal_id="svc", allowed_workspaces=("lab",))}
    )
    assert store.resolve(allowed).workspace_id == "lab"


def test_named_profiles(store: KnowledgeStore) -> None:
    store.set_profile(
        ScopeProfile(workspace_id="lab", profile_id="papers", research_types=["paper"], tags=["ml"])
    )
    _, records, _ = store.query_records(QueryContext(workspace_id="lab", profile_id="papers"))
    assert _ids(records) == {"r1", "r4"}
    # An explicit dimension overrides only that dimension; the profile's tags still apply.
    _, records, _ = store.query_records(
        QueryContext(workspace_id="lab", profile_id="papers", research_types=["note"])
    )
    assert _ids(records) == {"r2"}
    # The configured profile is used when the query names none.
    scope, records, _ = store.query_records(
        QueryContext(workspace_id="lab"), configured_profile="papers"
    )
    assert scope.profile_id == "papers" and _ids(records) == {"r1", "r4"}
    names = [p.profile_id for p in store.list_profiles("lab")]
    assert names == ["default", "papers"]
    with pytest.raises(UnknownScopeError):
        store.set_profile(
            ScopeProfile(workspace_id="lab", profile_id="bad", source_ids=["corpus:nope"])
        )


def test_research_type_vocabulary_matches_database_check(store: KnowledgeStore) -> None:
    for value in ResearchType:
        store.classify("notes:local", "r5", research_type=value.value)
    assert store.get_record("notes:local", "r5").research_type is ResearchType.OTHER
