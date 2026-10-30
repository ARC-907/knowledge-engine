"""HTTP, MCP and CLI bindings of evidence open and citation validation/export."""

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

DOC = "# Doc\n\nAlpha line.\n\n## Part\n\nBeta line.\n"
DOC_ID = corpus_record_id("libs/a/doc.md")
NOTE_ID = corpus_record_id("libs/a/note.txt")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    corpus = tmp_path / "corpus"
    (corpus / "libs/a").mkdir(parents=True)
    (corpus / "libs/a/doc.md").write_text(DOC, encoding="utf-8", newline="\n")
    (corpus / "libs/a/note.txt").write_text("note text\n", encoding="utf-8", newline="\n")
    registry = {
        "version": 1,
        "libraries": [{"id": "lib-a", "kind": "library", "name": "A", "path": "libs/a"}],
        "skills": [],
        "kits": [],
        "tools": [],
    }
    (corpus / "registry.json").write_text(json.dumps(registry), encoding="utf-8", newline="\n")
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    monkeypatch.setenv("KE_CONFIG_PATH", str(tmp_path / "absent.toml"))
    monkeypatch.chdir(tmp_path)
    backfill.apply(
        corpus_root=corpus,
        registry_path=corpus / "registry.json",
        db_path=data / "knowledge.db",
        workspace_id="default",
    )
    return {"corpus": corpus, "data": data, "db": data / "knowledge.db", "tmp": tmp_path}


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = cli.main(["knowledge", *argv])
    out, err = capsys.readouterr()
    try:
        parsed = json.loads(out) if out.strip() else None
    except ValueError:
        parsed = out
    return code, parsed, err


def _snapshot(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    code, report, err = _cli(capsys, "snapshot", "--apply")
    assert code == 0, err
    assert isinstance(report, dict)
    return report


def _revision(capsys: pytest.CaptureFixture[str], record_id: str) -> str:
    code, revs, _ = _cli(capsys, "evidence", "revisions", CORPUS_SOURCE_ID, record_id)
    assert code == 0
    return str(revs["current_revision_id"])


def _locator(text: str) -> dict[str, Any]:
    start = DOC.encode().index(text.encode())
    return {"kind": "text", "start_byte": start, "end_byte": start + len(text.encode())}


def _request(revision_id: str, **extra: Any) -> dict[str, Any]:
    ref = {
        "source_id": CORPUS_SOURCE_ID,
        "record_id": DOC_ID,
        "revision_id": revision_id,
        "locator": _locator("Beta line."),
    }
    return {
        "context": {"workspace_id": "default"},
        "claims": [
            {"claim_id": "q1", "kind": "quotation", "quote": "Beta line.", "evidence": [ref]},
            {"claim_id": "p1", "kind": "paraphrase", "text": "Beta is stated.", "evidence": [ref]},
        ],
        **extra,
    }


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_snapshot_open_cite_and_delete(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code, dry, _ = _cli(capsys, "snapshot", "--dry-run")
    assert code == 0 and dry["mode"] == "dry-run" and dry["first_revisions"] == 2
    report = _snapshot(capsys)
    assert report["revisions_created"] == 2
    revision = _revision(capsys, DOC_ID)
    code, opened, _ = _cli(
        capsys,
        "evidence",
        "open",
        "--source-id",
        CORPUS_SOURCE_ID,
        "--record-id",
        DOC_ID,
        "--revision-id",
        revision,
        "--locator",
        json.dumps(_locator("Alpha line.")),
    )
    assert code == 0 and opened["result"]["verification"] == "verified"
    assert opened["result"]["content"] == "Alpha line."

    request = env["tmp"] / "claims.json"
    request.write_text(json.dumps(_request(revision)), encoding="utf-8")
    code, validated, _ = _cli(capsys, "cite", "validate", str(request))
    assert code == 0 and validated["summary"]["resolved"] == 2
    assert "not proof" in validated["notice"]
    code, markdown, _ = _cli(
        capsys, "cite", "export", str(request), "--format", "markdown", "--raw"
    )
    assert code == 0 and isinstance(markdown, str)
    assert markdown.startswith("## Quotations") and "[1]" in markdown
    code, csl, _ = _cli(capsys, "cite", "export", str(request), "--format", "csl-json", "--raw")
    assert code == 0 and csl[0]["custom"]["revision_id"] == revision

    code, _, err = _cli(capsys, "evidence", "delete", CORPUS_SOURCE_ID, DOC_ID, "--reason", "x")
    assert code == 2 and "--yes" in err
    code, deleted, _ = _cli(
        capsys, "evidence", "delete", CORPUS_SOURCE_ID, DOC_ID, "--reason", "privacy", "--yes"
    )
    assert code == 0 and deleted["mode"] == "tombstone"
    code, after, _ = _cli(capsys, "cite", "validate", str(request))
    assert after["summary"]["unresolved"] == 2


def test_cli_errors(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _cli(capsys, "cite", "validate", str(env["tmp"] / "missing.json"))
    assert code == 2 and "cannot read" in err
    bad = env["tmp"] / "bad.json"
    bad.write_text(
        '{"context": {"workspace_id": "default"}, "claims": []}', encoding="utf-8", newline="\n"
    )
    code, _, err = _cli(capsys, "cite", "validate", str(bad))
    assert code == 2 and "error[invalid_input]" in err
    code, _, err = _cli(capsys, "evidence", "open", "--evidence-id", "ev_" + "1" * 32)
    assert code == 2 and "error[unknown_scope]" in err


def test_cli_snapshot_needs_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "nothing"))
    code, _, err = _cli(capsys, "snapshot", "--apply")
    assert code == 2 and "migrate --apply" in err


# ── HTTP ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> Iterator[TestClient]:
    from knowledge_engine.app import create_app

    _snapshot(capsys)
    with TestClient(create_app(Settings(board_enabled=False))) as test_client:
        yield test_client


def test_http_open_validate_export(client: TestClient) -> None:
    status = client.get("/knowledge/status").json()
    assert status["schema_version"] == 2 and status["evidence"]["revisions"] == 2
    revs = client.get(f"/knowledge/records/{CORPUS_SOURCE_ID}/{DOC_ID}/revisions").json()
    revision = revs["current_revision_id"]
    assert revs["revisions"][0]["current"] is True
    opened = client.post(
        "/knowledge/evidence/open",
        json={
            "context": {"workspace_id": "default"},
            "source_id": CORPUS_SOURCE_ID,
            "record_id": DOC_ID,
            "revision_id": revision,
            "locator": _locator("Beta line."),
        },
    )
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["result"]["verification"] == "verified" and body["evidence_id"].startswith("ev_")

    validated = client.post("/knowledge/citations/validate", json=_request(revision))
    assert validated.status_code == 200, validated.text
    assert validated.json()["summary"]["resolved"] == 2
    exported = client.post("/knowledge/citations/export", json=_request(revision, format="both"))
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["markdown"].count("[1]") >= 2 and len(payload["csl_json"]) == 1


def test_http_scope_denial_on_export(client: TestClient) -> None:
    revision = client.get(f"/knowledge/records/{CORPUS_SOURCE_ID}/{DOC_ID}/revisions").json()[
        "current_revision_id"
    ]
    client.post(
        "/knowledge/workspaces/default/projects", json={"display_name": "P", "project_id": "p"}
    )
    request = _request(revision)
    request["context"] = {"workspace_id": "default", "project_id": "p"}
    denied = client.post("/knowledge/citations/export", json=request)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "denied"
    invalid = client.post(
        "/knowledge/citations/validate", json={"context": {"workspace_id": "default"}}
    )
    assert invalid.status_code == 422


# ── MCP ──────────────────────────────────────────────────────────────


@pytest.fixture
def mcp_server(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> Iterator[Any]:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.knowledge.mcp_tools import close_all
    from knowledge_engine.mcp.server import build_server, load_knowledge_group
    from knowledge_engine.mcp.toolkit import register_group
    from knowledge_engine.retrieval.fts5 import Indexer

    _snapshot(capsys)
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


def test_mcp_cite_tools(mcp_server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    revision = _revision(capsys, DOC_ID)

    async def flow() -> list[Any]:
        async with Client(mcp_server) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            validated = await c.call_tool("knowledge_cite_validate", _request(revision))
            exported = await c.call_tool(
                "knowledge_cite_export", _request(revision, format="csl-json")
            )
            opened = await c.call_tool(
                "knowledge_evidence_open",
                {"context": {"workspace_id": "default"}, "evidence_id": "ev_" + "2" * 32},
            )
            return [names, validated, exported, opened]

    names, validated, exported, opened = anyio.run(flow)
    for name in ("knowledge_cite_validate", "knowledge_cite_export", "knowledge_evidence_open"):
        assert name in names
    assert json.loads(validated.content[0].text)["summary"]["resolved"] == 2
    assert json.loads(exported.content[0].text)["csl_json"][0]["type"] == "document"
    assert opened.is_error
    assert json.loads(opened.content[0].text)["error"]["code"] == "unknown_scope"
