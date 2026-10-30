"""The ``serena:<project>`` broker source, code evidence and citations, MCP and CLI surfaces."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from knowledge_engine.knowledge.broker import KnowledgeBroker
from knowledge_engine.knowledge.contracts import (
    CodeLocator,
    QueryContext,
    VerificationState,
)
from knowledge_engine.knowledge.errors import UnavailableError, UnknownScopeError
from knowledge_engine.knowledge.evidence.citations import CitationRequest, validate_claims
from knowledge_engine.knowledge.evidence.store import EvidenceStore
from knowledge_engine.knowledge.sources import build_adapter_factory
from knowledge_engine.knowledge.store import KnowledgeStore
from knowledge_engine.serena.adapter import (
    SerenaSourceAdapter,
    register_sources,
    source_id_for,
)
from knowledge_engine.serena.manager import SerenaManager
from knowledge_engine.serena.mcp_tools import collect_tools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

SOURCE_A = source_id_for("proj-a")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[KnowledgeStore]:
    with KnowledgeStore.open(tmp_path / "knowledge.db") as opened:
        opened.create_workspace("Workspace", workspace_id="ws")
        opened.create_project("ws", "A", project_id="proj-a")
        opened.create_project("ws", "B", project_id="proj-b")
        yield opened


@pytest.fixture
def manager(make_manager: Callable[..., SerenaManager]) -> SerenaManager:
    return make_manager()


@pytest.fixture
def broker(store: KnowledgeStore, manager: SerenaManager) -> Iterator[KnowledgeBroker]:
    register_sources(store, manager.config)
    opened = KnowledgeBroker(store, build_adapter_factory(None, serena=lambda: manager))
    yield opened
    opened.close()


def _ctx(**fields: Any) -> QueryContext:
    return QueryContext(workspace_id="ws", **fields)


def _adapter(
    store: KnowledgeStore, manager: SerenaManager, context: QueryContext
) -> tuple[SerenaSourceAdapter, Any, EvidenceStore]:
    snapshot = store.snapshot(context)
    evidence = EvidenceStore(store)
    adapter = SerenaSourceAdapter(SOURCE_A, snapshot, lambda: manager, evidence=evidence)
    return adapter, snapshot.scope, evidence


def test_sources_are_registered_per_configured_project(
    store: KnowledgeStore, manager: SerenaManager
) -> None:
    first = register_sources(store, manager.config)
    assert first == {"registered": ["serena:proj-a", "serena:proj-b"], "skipped": []}
    again = register_sources(store, manager.config)  # idempotent
    assert again["registered"] == first["registered"]
    source = store.get_source(SOURCE_A)
    assert (source.kind, source.workspace_id) == ("serena", "ws")
    assert store.source_config(SOURCE_A) == {"project_id": "proj-a"}


def test_unknown_projects_are_skipped(
    store: KnowledgeStore, make_manager: Callable[..., SerenaManager], projects: dict[str, Any]
) -> None:
    from dataclasses import replace

    manager = make_manager()
    extra = replace(projects["proj-a"], project_id="proj-z")
    config = replace(manager.config, projects={**manager.config.projects, "proj-z": extra})
    result = register_sources(store, config)
    assert result["skipped"][0]["project_id"] == "proj-z"


def test_explicit_selection_searches_code_symbols(
    broker: KnowledgeBroker, kit: SimpleNamespace, tmp_path: Path
) -> None:
    envelope = broker.search(_ctx(source_ids=[SOURCE_A]), "Shape")
    assert not envelope.partial
    titles = [hit.title for hit in envelope.hits]
    assert any(t.startswith("Shape (Class) in pkg/shapes.py") for t in titles)
    hit = envelope.hits[0]
    assert hit.scope.project_id == "proj-a"
    assert isinstance(hit.locator, CodeLocator)
    assert hit.locator.commit == kit.git(tmp_path / "repo-a", "rev-parse", "HEAD")
    assert hit.revision_id is not None and hit.excerpt.startswith("class Shape")


def test_unselected_sources_stay_silent_and_start_nothing(
    broker: KnowledgeBroker, manager: SerenaManager, call_log: Path, kit: SimpleNamespace
) -> None:
    for mode in ("fts", "vector", "hybrid"):
        envelope = broker.search(_ctx(mode=mode), "Shape")
        assert envelope.hits == () and not envelope.partial
    assert kit.read_log(call_log) == []
    assert all(p["state"] == "stopped" for p in manager.status()["projects"])


def test_explicit_vector_search_is_unavailable(broker: KnowledgeBroker) -> None:
    with pytest.raises(UnavailableError):
        broker.search(_ctx(source_ids=[SOURCE_A], mode="vector"), "Shape")


def test_project_scope_isolates_projects(broker: KnowledgeBroker) -> None:
    both = [source_id_for("proj-a"), source_id_for("proj-b")]
    only_b = broker.search(_ctx(source_ids=both, project_id="proj-b"), "Shape")
    assert {hit.source_id for hit in only_b.hits} == {"serena:proj-b"}
    assert {hit.locator.repository_id for hit in only_b.hits} == {"example.org/b"}  # type: ignore[union-attr]
    tagged = broker.search(_ctx(source_ids=both, tags=["needs-tag"]), "Shape")
    assert tagged.hits == ()


def test_open_verifies_records_evidence_and_cites(
    store: KnowledgeStore, manager: SerenaManager
) -> None:
    register_sources(store, manager.config)
    context = _ctx(source_ids=[SOURCE_A])
    adapter, scope, evidence = _adapter(store, manager, context)
    hit = next(h for h in adapter.search(context, scope, "area").hits)
    assert isinstance(hit.locator, CodeLocator) and hit.revision_id is not None
    opened = adapter.open(context, scope, hit.record_id, hit.revision_id, hit.locator)
    assert opened.verification == VerificationState.VERIFIED
    assert opened.content is not None and opened.content.startswith("    def area")
    # The opened revision is now evidence of the project: the citation service resolves it.
    record = store.get_record(SOURCE_A, hit.record_id)
    assert record.scope.project_id == "proj-a" and record.logical_path == "pkg/shapes.py"
    request = CitationRequest(
        context={"workspace_id": "ws"},
        claims=(
            {
                "claim_id": "c1",
                "kind": "quotation",
                "quote": opened.content,
                "evidence": [
                    {
                        "source_id": SOURCE_A,
                        "record_id": hit.record_id,
                        "revision_id": hit.revision_id,
                        "locator": hit.locator.model_dump(mode="json"),
                    }
                ],
            },
        ),
    )
    report = validate_claims(evidence, request)
    assert report.claims[0].status == "resolved", report


def test_dirty_tree_is_cited_by_snapshot_not_commit(
    store: KnowledgeStore, manager: SerenaManager, projects: dict[str, Any]
) -> None:
    register_sources(store, manager.config)
    root = projects["proj-a"].root
    (root / "pkg" / "shapes.py").write_bytes(
        b"class Shape:\n    def area(self):\n        return 42\n"
    )
    manager.repo_states.clear()
    context = _ctx(source_ids=[SOURCE_A])
    adapter, scope, evidence = _adapter(store, manager, context)
    hit = next(h for h in adapter.search(context, scope, "area").hits)
    locator = hit.locator
    assert isinstance(locator, CodeLocator)
    assert locator.commit is None and locator.snapshot_digest is not None
    opened = adapter.open(context, scope, hit.record_id, hit.revision_id or "", locator)
    assert opened.verification == VerificationState.VERIFIED
    assert "return 42" in (opened.content or "")
    info = evidence.get_revision(hit.revision_id or "")
    assert info is not None and info.revision.upstream_version == locator.snapshot_digest
    # A locator claiming the clean commit for the dirty bytes does not verify.
    commit = manager.repo_states.get(root)
    assert commit.commit is None
    forged = locator.model_copy(update={"commit": "0" * 40, "snapshot_digest": None})
    refused = adapter.open(context, scope, hit.record_id, hit.revision_id or "", forged)
    assert refused.verification == VerificationState.UNSUPPORTED


def test_changed_files_never_serve_newer_content(
    store: KnowledgeStore, manager: SerenaManager, projects: dict[str, Any]
) -> None:
    register_sources(store, manager.config)
    context = _ctx(source_ids=[SOURCE_A])
    adapter, scope, _ = _adapter(store, manager, context)
    hit = next(h for h in adapter.search(context, scope, "Shape").hits)
    old_revision = hit.revision_id or ""
    adapter.open(context, scope, hit.record_id, old_revision)  # records the revision
    (projects["proj-a"].root / "pkg" / "shapes.py").write_bytes(b"class Shape:\n    pass\n")
    manager.repo_states.clear()
    stale = adapter.open(context, scope, hit.record_id, old_revision)
    assert stale.verification == VerificationState.STALE
    assert stale.content is not None and "def area" in stale.content
    never = adapter.open(context, scope, hit.record_id, "rev_" + "0" * 32)
    assert never.verification == VerificationState.UNSUPPORTED and never.content is None


def test_open_outside_the_scope_is_unknown(store: KnowledgeStore, manager: SerenaManager) -> None:
    register_sources(store, manager.config)
    context = _ctx(source_ids=[SOURCE_A], project_id="proj-b")
    adapter, scope, _ = _adapter(store, manager, context)
    with pytest.raises(UnknownScopeError):
        adapter.open(context, scope, "f.obqws4dfnb2gc4dfomxha6i", "rev_" + "0" * 32)


def test_disabled_adapter_reports_unavailable_sources(
    store: KnowledgeStore, manager: SerenaManager
) -> None:
    register_sources(store, manager.config)
    broker = KnowledgeBroker(store, build_adapter_factory(None, serena=None))
    try:
        with pytest.raises(UnavailableError):
            broker.search(_ctx(source_ids=[SOURCE_A]), "Shape")
    finally:
        broker.close()


# ── MCP and CLI ──────────────────────────────────────────────────────


def _payload(envelope: dict[str, Any]) -> Any:
    return json.loads(envelope["content"][0]["text"])


def test_mcp_group_runs_read_tools_and_refuses_others(manager: SerenaManager) -> None:
    defs, dispatch, context = collect_tools(lambda: manager)
    names = {d["name"] for d in defs}
    assert names == {
        "serena.find_symbol",
        "serena.get_symbols_overview",
        "serena.find_referencing_symbols",
        "serena.status",
    }
    found = dispatch["serena.find_symbol"](
        "serena.find_symbol", {"project_id": "proj-a", "name_path_pattern": "Shape"}, context
    )
    assert "isError" not in found
    assert _payload(found)["results"][0]["path"] == "pkg/shapes.py"
    refused = dispatch["serena.find_symbol"](
        "serena.execute_shell_command", {"project_id": "proj-a"}, context
    )
    assert refused["isError"] is True
    assert _payload(refused)["error"]["code"] == "invalid_input"
    bad = dispatch["serena.find_symbol"]("serena.find_symbol", {"project_id": "proj-a"}, context)
    assert _payload(bad)["error"]["code"] == "invalid_input"
    status = _payload(dispatch["serena.status"]("serena.status", {}, context))
    assert status["expected_version"] == "2.1.0"


def test_mcp_group_is_absent_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.mcp.server import GroupDisabledError, load_serena_group

    with pytest.raises(GroupDisabledError):
        load_serena_group()


def test_cli_status_and_disabled_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    projects: dict[str, Any],
) -> None:
    from knowledge_engine import cli

    code = cli.main(["serena", "status"])
    assert code == 4
    assert "KE_SERENA_ENABLED=1" in capsys.readouterr().err
    projects_file = tmp_path / "serena.json"
    projects_file.write_bytes(
        json.dumps(
            {"projects": [{"project_id": "proj-a", "root": str(projects["proj-a"].root)}]}
        ).encode()
    )
    monkeypatch.setenv("KE_SERENA_ENABLED", "1")
    monkeypatch.setenv("KE_SERENA_PROJECTS", str(projects_file))
    assert cli.main(["serena", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert [p["project_id"] for p in status["projects"]] == ["proj-a"]
    assert status["projects"][0]["state"] == "stopped"
