"""HTTP (``/knowledge``), MCP (``knowledge_*``) and CLI bindings of the knowledge admin service."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
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
from knowledge_engine.knowledge.service import OPERATIONS


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    corpus = tmp_path / "corpus"
    (corpus / "libs/a").mkdir(parents=True)
    (corpus / "libs/a/one.md").write_text("# One\nalpha\n", encoding="utf-8")
    (corpus / "libs/a/two.md").write_text("# Two\nbeta\n", encoding="utf-8")
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
    monkeypatch.chdir(tmp_path)
    return {"corpus": corpus, "data": data, "db": data / "knowledge.db"}


def _migrate(env: dict[str, Path]) -> None:
    backfill.apply(
        corpus_root=env["corpus"],
        registry_path=env["corpus"] / "registry.json",
        db_path=env["db"],
        workspace_id="default",
    )


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    from knowledge_engine.app import create_app

    _migrate(env)
    with TestClient(create_app(Settings(board_enabled=False))) as test_client:
        yield test_client


ONE = corpus_record_id("libs/a/one.md")
TWO = corpus_record_id("libs/a/two.md")


# ── HTTP ─────────────────────────────────────────────────────────────


def test_http_admin_flow(client: TestClient) -> None:
    status = client.get("/knowledge/status").json()
    assert status["counts"]["records"] == 2 and status["counts"]["workspaces"] == 1

    p1 = client.post("/knowledge/workspaces/default/projects", json={"display_name": "Thesis"})
    p2 = client.post("/knowledge/workspaces/default/projects", json={"display_name": "Thesis"})
    assert p1.status_code == p2.status_code == 200
    assert p1.json()["project_id"] != p2.json()["project_id"]
    alpha = p1.json()["project_id"]

    c1 = client.post("/knowledge/workspaces/default/collections", json={"display_name": "ML"})
    c2 = client.post(
        "/knowledge/workspaces/default/collections",
        json={"display_name": "Reading", "collection_id": "reading"},
    )
    assert c2.json()["collection_id"] == "reading"
    for cid in (c1.json()["collection_id"], "reading"):
        added = client.post(
            f"/knowledge/collections/{cid}/members",
            json={"records": [{"source_id": CORPUS_SOURCE_ID, "record_id": ONE}]},
        )
        assert added.json()["added"] == 1

    classified = client.patch(
        f"/knowledge/records/{CORPUS_SOURCE_ID}/{ONE}",
        json={"research_type": "paper", "curation_status": "reviewed", "tags": ["ML"]},
    )
    assert classified.status_code == 200, classified.text
    body = classified.json()
    assert body["research_type"] == "paper" and body["tags"] == ["ml"]
    assert len(body["collection_ids"]) == 2

    moved = client.patch(f"/knowledge/records/{CORPUS_SOURCE_ID}/{TWO}", json={"project_id": alpha})
    assert moved.json()["scope"] == {"workspace_id": "default", "project_id": alpha}

    page = client.post(
        "/knowledge/records/query",
        json={"context": {"workspace_id": "default", "project_id": alpha}},
    ).json()
    assert [r["record_id"] for r in page["records"]] == [TWO]
    assert page["effective_scope"]["project_id"] == alpha

    listed = client.get("/knowledge/workspaces/default/collections").json()["collections"]
    assert {c["member_count"] for c in listed} == {1}
    assert len(client.get("/knowledge/workspaces/default/projects").json()["projects"]) == 2
    assert client.get("/knowledge/workspaces/default/sources").json()["sources"][0][
        "source_id"
    ] == (CORPUS_SOURCE_ID)

    prof = client.put(
        "/knowledge/workspaces/default/profiles/papers", json={"research_types": ["paper"]}
    )
    assert prof.status_code == 200 and prof.json()["profile_id"] == "papers"
    page = client.post(
        "/knowledge/records/query",
        json={"context": {"workspace_id": "default", "profile_id": "papers"}},
    ).json()
    assert [r["record_id"] for r in page["records"]] == [ONE]
    names = [
        p["profile_id"]
        for p in client.get("/knowledge/workspaces/default/profiles").json()["profiles"]
    ]
    assert names == ["default", "papers"]

    removed = client.post(
        "/knowledge/collections/reading/members/remove",
        json={"records": [{"source_id": CORPUS_SOURCE_ID, "record_id": ONE}]},
    )
    assert removed.json() == {"collection_id": "reading", "added": 0, "removed": 1, "unchanged": 0}


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code"),
    [
        (
            "post",
            "/knowledge/records/query",
            {"context": {"workspace_id": "nope"}},
            422,
            "unknown_scope",
        ),
        (
            "post",
            "/knowledge/records/query",
            {"context": {"workspace_id": "default", "source_ids": ["corpus:nope"]}},
            422,
            "unknown_scope",
        ),
        (
            "post",
            "/knowledge/records/query",
            {"context": {"workspace_id": "default", "principal": {"principal_id": "x"}}},
            400,
            "invalid_input",
        ),
        ("get", "/knowledge/workspaces/nope/projects", None, 422, "unknown_scope"),
        ("post", "/knowledge/workspaces/BAD/projects", {"display_name": "x"}, 400, "invalid_input"),
        (
            "patch",
            f"/knowledge/records/{CORPUS_SOURCE_ID}/{ONE}",
            {"research_type": "podcast"},
            422,
            None,
        ),
        (
            "patch",
            f"/knowledge/records/{CORPUS_SOURCE_ID}/missing",
            {"research_type": "note"},
            422,
            "unknown_scope",
        ),
        (
            "post",
            "/knowledge/workspaces",
            {"display_name": "Again", "workspace_id": "default"},
            400,
            "invalid_input",
        ),
    ],
)
def test_http_errors_are_typed(
    client: TestClient, method: str, path: str, body: Any, status: int, code: str | None
) -> None:
    response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert response.status_code == status, response.text
    if code is not None:
        payload = response.json()
        assert payload["type"] == code
        assert payload["error"]["code"] == code
        assert payload["detail"] == payload["error"]["message"]


def test_explicit_empty_selection_over_http(client: TestClient) -> None:
    page = client.post(
        "/knowledge/records/query",
        json={"context": {"workspace_id": "default", "source_ids": []}},
    ).json()
    assert page["records"] == [] and page["total"] == 0 and page["effective_scope"]["empty"]


def test_knowledge_routes_can_be_disabled(env: dict[str, Path]) -> None:
    from knowledge_engine.app import create_app

    with TestClient(create_app(Settings(board_enabled=False, knowledge_enabled=False))) as c:
        assert c.get("/knowledge/status").status_code == 404
    assert not env["db"].exists()


def test_store_failure_leaves_the_app_up(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.app import create_app

    env["data"].mkdir(parents=True)
    env["db"].mkdir()  # a directory where the database file should be
    with TestClient(create_app(Settings(board_enabled=False))) as c:
        assert c.get("/health").status_code == 200
        response = c.get("/knowledge/status")
        assert response.status_code == 503


def test_legacy_search_is_unchanged_by_knowledge_metadata(client: TestClient) -> None:
    client.post("/search/reindex")
    before = client.get("/search", params={"q": "alpha"}).json()
    client.patch(
        f"/knowledge/records/{CORPUS_SOURCE_ID}/{ONE}",
        json={"research_type": "paper", "tags": ["x"], "curation_status": "archived"},
    )
    after = client.get("/search", params={"q": "alpha"}).json()
    assert before == after and after["count"] == 1


# ── MCP ──────────────────────────────────────────────────────────────


def _with_client(server: Any, fn: Callable[[Client], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        async with Client(server) as mcp_client:
            return await fn(mcp_client)

    return anyio.run(main)


@pytest.fixture
def mcp_server(env: dict[str, Path]) -> Iterator[Any]:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.knowledge.mcp_tools import close_all
    from knowledge_engine.mcp.server import build_server, load_knowledge_group
    from knowledge_engine.mcp.toolkit import register_group
    from knowledge_engine.retrieval.fts5 import Indexer

    _migrate(env)
    config = Config.from_env()
    registry = Registry(config.registry_path)
    indexer = Indexer(config, registry)
    server = build_server(config, registry, indexer, include_groups=False)
    group = load_knowledge_group()
    register_group(server, group.schemas, group.dispatch, group=group.name, context=group.context)
    try:
        yield server
    finally:
        close_all()
        indexer.close()
        registry.close()


def test_mcp_tools_cover_every_operation(mcp_server: Any) -> None:
    async def names(c: Client) -> list[str]:
        return [t.name for t in (await c.list_tools()).tools]

    listed = _with_client(mcp_server, names)
    assert [n for n in listed if n.startswith("knowledge_")] == [
        "knowledge_" + op.name for op in OPERATIONS
    ]


def test_mcp_admin_calls_and_typed_errors(mcp_server: Any) -> None:
    async def flow(c: Client) -> list[Any]:
        return [
            await c.call_tool(
                "knowledge_project_create", {"workspace_id": "default", "display_name": "T"}
            ),
            await c.call_tool(
                "knowledge_record_classify",
                {"source_id": CORPUS_SOURCE_ID, "record_id": ONE, "research_type": "dataset"},
            ),
            await c.call_tool(
                "knowledge_record_query",
                {"context": {"workspace_id": "default", "research_types": ["dataset"]}},
            ),
            await c.call_tool("knowledge_record_query", {"context": {"workspace_id": "nope"}}),
            await c.call_tool("knowledge_project_list", {"workspace_id": 5}),
        ]

    created, classified, queried, unknown, invalid = _with_client(mcp_server, flow)
    assert not created.is_error and json.loads(created.content[0].text)["display_name"] == "T"
    assert json.loads(classified.content[0].text)["research_type"] == "dataset"
    assert [r["record_id"] for r in json.loads(queried.content[0].text)["records"]] == [ONE]
    assert unknown.is_error
    assert json.loads(unknown.content[0].text)["error"]["code"] == "unknown_scope"
    assert invalid.is_error


def test_mcp_group_respects_the_toggle(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.mcp.server import GroupDisabledError, load_knowledge_group

    monkeypatch.setenv("KE_KNOWLEDGE_ENABLED", "0")
    with pytest.raises(GroupDisabledError):
        load_knowledge_group()


def test_mcp_listing_does_not_open_the_store(env: dict[str, Path]) -> None:
    from knowledge_engine.mcp.server import load_knowledge_group

    load_knowledge_group()
    assert not env["db"].exists()


# ── CLI and parity ───────────────────────────────────────────────────


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = cli.main(["knowledge", *argv])
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else None, err


def test_cli_admin_flow(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _migrate(env)
    code, ws, _ = _cli(capsys, "workspace", "create", "Second", "--id", "second")
    assert code == 0 and ws["workspace_id"] == "second"
    code, listed, _ = _cli(capsys, "workspace", "list")
    assert [w["workspace_id"] for w in listed["workspaces"]] == ["default", "second"]
    code, prj, _ = _cli(capsys, "project", "create", "default", "Thesis", "--id", "thesis")
    assert prj["project_id"] == "thesis"
    code, col, _ = _cli(capsys, "collection", "create", "default", "Methods", "--project", "thesis")
    cid = col["collection_id"]
    code, added, _ = _cli(capsys, "collection", "add", cid, "--record", CORPUS_SOURCE_ID, ONE)
    assert added["added"] == 1
    code, rec, _ = _cli(
        capsys, "record", "classify", CORPUS_SOURCE_ID, ONE, "--type", "note", "--add-tag", "Draft"
    )
    assert rec["research_type"] == "note" and rec["tags"] == ["draft"]
    code, page, _ = _cli(capsys, "record", "list", "--tag", "draft")
    assert [r["record_id"] for r in page["records"]] == [ONE]
    code, page, _ = _cli(
        capsys, "record", "list", "--context", '{"workspace_id": "default", "source_ids": []}'
    )
    assert page["total"] == 0
    code, _, _ = _cli(capsys, "profile", "set", "default", "notes", "--type", "note")
    code, profiles, _ = _cli(capsys, "profile", "list", "default")
    assert [p["profile_id"] for p in profiles["profiles"]] == ["default", "notes"]
    code, sources, _ = _cli(capsys, "source", "list", "default")
    assert sources["sources"][0]["kind"] == "corpus"
    code, cols, _ = _cli(capsys, "collection", "list", "default", "--project", "thesis")
    assert [c["collection_id"] for c in cols["collections"]] == [cid]
    code, removed, _ = _cli(capsys, "collection", "remove", cid, "--record", CORPUS_SOURCE_ID, ONE)
    assert removed["removed"] == 1


@pytest.mark.parametrize(
    ("argv", "exit_code", "code"),
    [
        (["project", "list", "nope"], 2, "unknown_scope"),
        (["workspace", "create", "X", "--id", "Bad Id"], 2, "invalid_input"),
        (["record", "list", "--source", "corpus:nope"], 2, "unknown_scope"),
        (["record", "list", "--context", "[1]"], 2, None),
    ],
)
def test_cli_errors(
    env: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    exit_code: int,
    code: str | None,
) -> None:
    _migrate(env)
    status, _, err = _cli(capsys, *argv)
    assert status == exit_code
    if code is not None:
        assert f"error[{code}]" in err


def test_three_surfaces_return_the_same_records(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str], mcp_server: Any
) -> None:
    context = {"workspace_id": "default", "tags": []}
    _, cli_page, _ = _cli(capsys, "record", "list", "--context", json.dumps(context))

    async def call(c: Client) -> Any:
        return await c.call_tool("knowledge_record_query", {"context": context})

    mcp_page = json.loads(_with_client(mcp_server, call).content[0].text)
    from knowledge_engine.app import create_app

    with TestClient(create_app(Settings(board_enabled=False))) as c:
        http_page = c.post("/knowledge/records/query", json={"context": context}).json()
    assert cli_page == mcp_page == http_page
    assert {r["record_id"] for r in http_page["records"]} == {ONE, TWO}
