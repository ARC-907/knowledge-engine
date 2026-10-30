"""HTTP (``/knowledge/search``, ``/knowledge/open``), MCP (``knowledge.search``,
``knowledge.open``) and CLI (``knowledge search|open``) bindings of the broker: one
service, the same JSON and the same typed errors on every surface."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from knowledge_engine import cli
from knowledge_engine.core.settings import Settings
from knowledge_engine.knowledge import backfill
from knowledge_engine.knowledge.backfill import CORPUS_SOURCE_ID, corpus_record_id

OWL = corpus_record_id("libs/a/owl.md")
FOX = corpus_record_id("libs/a/fox.md")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    corpus = tmp_path / "corpus"
    (corpus / "libs/a").mkdir(parents=True)
    (corpus / "libs/a/owl.md").write_text("# Owls\n\nOwls hunt at night.\n", encoding="utf-8")
    (corpus / "libs/a/fox.md").write_text("# Foxes\n\nFoxes hunt mice.\n", encoding="utf-8")
    registry = {
        "version": 1,
        "libraries": [{"id": "lib-a", "kind": "library", "name": "A", "path": "libs/a"}],
        "skills": [],
        "kits": [],
        "tools": [],
    }
    (corpus / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    monkeypatch.setenv("KE_CONFIG_PATH", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("KE_EMBED_PROVIDER", "fake")
    monkeypatch.chdir(tmp_path)
    backfill.apply(
        corpus_root=corpus,
        registry_path=corpus / "registry.json",
        db_path=data / "knowledge.db",
        workspace_id="default",
    )
    assert cli.main(["reindex"]) == 0
    from knowledge_engine.knowledge.store import KnowledgeStore

    with KnowledgeStore.open(data / "knowledge.db") as store:
        store.create_project("default", "Thesis", project_id="thesis")
        store.classify(CORPUS_SOURCE_ID, FOX, project_id="thesis", add_tags=["mammal"])
    return {"corpus": corpus, "data": data}


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    from knowledge_engine.app import create_app

    with TestClient(create_app(Settings(board_enabled=False))) as test_client:
        yield test_client


@pytest.fixture
def mcp_server(env: dict[str, Path]) -> Iterator[Any]:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.knowledge.broker_mcp import close_all
    from knowledge_engine.mcp.server import build_server, load_knowledge_broker_group
    from knowledge_engine.mcp.toolkit import register_group
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    registry = Registry(config.registry_path)
    indexer = Indexer(config, registry)
    server = build_server(config, registry, indexer, include_groups=False)
    group = load_knowledge_broker_group()
    register_group(server, group.schemas, group.dispatch, group=group.name, context=group.context)
    try:
        yield server
    finally:
        close_all()
        indexer.close()
        registry.close()


def _call(server: Any, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    async def main() -> Any:
        async with Client(server) as mcp_client:
            return await mcp_client.call_tool(name, arguments)

    result = anyio.run(main)
    return bool(result.is_error), json.loads(result.content[0].text)


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = cli.main(["knowledge", *argv])
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else None, err


def _stable(envelope: dict[str, Any]) -> dict[str, Any]:
    for status in envelope.get("source_status", ()):
        status["elapsed_ms"] = None
    return envelope


SCOPED = {"workspace_id": "default", "project_id": "thesis", "tags": ["mammal"]}


def test_three_surfaces_return_the_same_envelope(
    client: TestClient, mcp_server: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    http = client.post("/knowledge/search", json={"query": "hunt", "context": SCOPED})
    assert http.status_code == 200, http.text
    is_error, mcp_body = _call(mcp_server, "knowledge.search", {"query": "hunt", "context": SCOPED})
    assert not is_error
    code, cli_body, _ = _cli(capsys, "search", "hunt", "--project", "thesis", "--tag", "mammal")
    assert code == 0
    assert _stable(http.json()) == _stable(mcp_body) == _stable(cli_body)
    body = http.json()
    assert [h["record_id"] for h in body["hits"]] == [FOX]
    assert body["effective_scope"]["project_id"] == "thesis"
    assert body["contract_version"] == 1 and body["partial"] is False

    wide = {"workspace_id": "default"}
    http_wide = client.post("/knowledge/search", json={"query": "hunt", "context": wide}).json()
    _, mcp_wide = _call(mcp_server, "knowledge.search", {"query": "hunt", "context": wide})
    _, cli_wide, _ = _cli(capsys, "search", "hunt")
    assert _stable(http_wide) == _stable(mcp_wide) == _stable(cli_wide)
    assert sorted(h["record_id"] for h in http_wide["hits"]) == sorted([FOX, OWL])


def test_open_parity(
    client: TestClient, mcp_server: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    hit = client.post(
        "/knowledge/search", json={"query": "owls", "context": {"workspace_id": "default"}}
    ).json()["hits"][0]
    request = {
        "context": {"workspace_id": "default"},
        "source_id": CORPUS_SOURCE_ID,
        "record_id": hit["record_id"],
        "revision_id": hit["revision_id"],
    }
    posted = client.post("/knowledge/open", json=request)
    assert posted.status_code == 200, posted.text
    got = client.get(
        "/knowledge/open",
        params={
            "workspace_id": "default",
            "source_id": CORPUS_SOURCE_ID,
            "record_id": hit["record_id"],
        },
    )
    _, mcp_body = _call(mcp_server, "knowledge.open", request)
    code, cli_body, _ = _cli(
        capsys, "open", CORPUS_SOURCE_ID, hit["record_id"], "--revision", hit["revision_id"]
    )
    assert code == 0
    assert posted.json() == got.json() == mcp_body == cli_body
    assert posted.json()["verification"] == "verified"
    assert posted.json()["content"].startswith("# Owls")


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"query": "x", "context": {"workspace_id": "nope"}}, 422, "unknown_scope"),
        (
            {"query": "x", "context": {"workspace_id": "default", "source_ids": ["corpus:nope"]}},
            422,
            "unknown_scope",
        ),
        (
            {
                "query": "x",
                "context": {"workspace_id": "default", "principal": {"principal_id": "u"}},
            },
            400,
            "invalid_input",
        ),
        ({"query": "x", "context": {"workspace_id": "default", "limit": 0}}, 400, "invalid_input"),
        (
            {"query": "owls", "context": {"workspace_id": "default", "mode": "vector"}},
            503,
            "unavailable",
        ),
        ({"context": {"workspace_id": "default"}}, 422, None),
    ],
)
def test_http_search_errors_are_typed(
    client: TestClient, body: dict[str, Any], status: int, code: str | None
) -> None:
    response = client.post("/knowledge/search", json=body)
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["error"]["code"] == code


def test_http_open_errors_are_typed(client: TestClient) -> None:
    response = client.get(
        "/knowledge/open",
        params={"workspace_id": "default", "source_id": CORPUS_SOURCE_ID, "record_id": "file-x"},
    )
    assert response.status_code == 422 and response.json()["type"] == "unknown_scope"
    # A project record is invisible to another project's scope.
    response = client.get(
        "/knowledge/open",
        params={
            "workspace_id": "default",
            "project_id": "thesis",
            "source_id": CORPUS_SOURCE_ID,
            "record_id": OWL,
        },
    )
    assert response.status_code == 422


def test_mcp_and_cli_errors_are_typed(mcp_server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    is_error, body = _call(
        mcp_server, "knowledge.search", {"query": "x", "context": {"workspace_id": "nope"}}
    )
    assert is_error and body["error"]["code"] == "unknown_scope"
    is_error, body = _call(
        mcp_server,
        "knowledge.search",
        {"query": "owls", "context": {"workspace_id": "default", "mode": "vector"}},
    )
    assert is_error and body["error"]["code"] == "unavailable"
    code, _, err = _cli(capsys, "search", "x", "--workspace", "nope")
    assert code == 2 and "error[unknown_scope]" in err
    code, _, err = _cli(capsys, "search", "owls", "--mode", "vector")
    assert code == 4 and "error[unavailable]" in err
    code, _, err = _cli(capsys, "open", CORPUS_SOURCE_ID, "file-x")
    assert code == 2 and "error[unknown_scope]" in err


def test_project_context_link_surfaces(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    response = client.put(
        "/knowledge/workspaces/default/project-context/links/thesis",
        json={"root": str(tmp_path / "missing")},
    )
    assert response.status_code == 400 and response.json()["type"] == "invalid_input"
    code, _, err = _cli(capsys, "project-context", "link", "default", "thesis", "relative")
    assert code == 2 and "error[invalid_input]" in err
    code, _, err = _cli(capsys, "project-context", "unlink", "default", "thesis")
    assert code == 2 and "error[unknown_scope]" in err  # no project-context source yet
    response = client.delete("/knowledge/workspaces/default/project-context/links/thesis")
    assert response.status_code == 422


def test_broker_surfaces_can_be_disabled(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.app import create_app
    from knowledge_engine.mcp.server import GroupDisabledError, load_knowledge_broker_group

    with TestClient(create_app(Settings(board_enabled=False, knowledge_enabled=False))) as c:
        assert c.post("/knowledge/search", json={"query": "x", "context": {}}).status_code == 404
    monkeypatch.setenv("KE_KNOWLEDGE_ENABLED", "0")
    with pytest.raises(GroupDisabledError):
        load_knowledge_broker_group()


def test_listing_the_broker_tools_opens_nothing(env: dict[str, Path]) -> None:
    from knowledge_engine.knowledge.broker_mcp import close_all
    from knowledge_engine.mcp.server import load_knowledge_broker_group

    group = load_knowledge_broker_group()
    assert [tool["name"] for tool in group.schemas] == ["knowledge.search", "knowledge.open"]
    assert group.context._engine is None
    close_all()


def test_legacy_search_is_unchanged(client: TestClient) -> None:
    legacy = client.get("/search", params={"q": "hunt"}).json()
    assert legacy["count"] == 2 and set(legacy["results"][0]) >= {"entry_id", "path", "score"}
