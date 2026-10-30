"""The two reference modules through MCP, HTTP and the CLI -- with no loader edits.

Adding a module is a manifest plus operator configuration: these tests
register and enable modules at runtime and call them on every surface while
``mcp.server.GROUP_LOADERS`` holds one generic ``modules`` entry.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

LOOPBACK = ("127.0.0.1", 50000)
INTERNET = ("203.0.113.9", 50000)
MCP_ECHO = Path(__file__).resolve().parents[1] / "fixtures" / "modules" / "mcp-echo"

_OPENED: list[Any] = []


@pytest.fixture(autouse=True)
def _close_engines() -> Any:
    yield
    while _OPENED:
        _OPENED.pop().close()


def _server() -> Any:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.mcp.server import build_server
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    indexer = Indexer(config, registry)
    _OPENED.extend([registry, indexer])
    return build_server(config, registry, indexer)


def _client(server: Any, fn: Callable[[Client], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        async with Client(server) as client:
            return await fn(client)

    return anyio.run(main)


def _payload(result: Any) -> Any:
    return json.loads(result.content[0].text)


def _use_as_default(host: Any) -> None:
    from knowledge_engine.modules.dispatcher import set_default_host

    set_default_host(host)


# ── MCP ──────────────────────────────────────────────────────────────


def test_group_loader_list_is_generic() -> None:
    from knowledge_engine.mcp.server import GROUP_LOADERS

    assert [name for name, _ in GROUP_LOADERS] == [
        "project_context",
        "agent_board",
        "knowledge",
        "knowledge_broker",
        "modules",
        "serena",
    ]


def test_modules_are_mcp_tools_without_loader_edits(mod_env: Any, host: Any, helpers: Any) -> None:
    helpers.enable(host, mod_env.news, fixture_roots=["fixtures", "../transcription/fixtures"])
    helpers.enable(host, mod_env.transcription)
    _use_as_default(host)
    server = _server()
    news_uri = mod_env.fixture_uri("news-fetcher", "solar-storage.html")
    audio_uri = mod_env.fixture_uri("transcription", "interview.txt")

    async def scenario(client: Client) -> dict[str, Any]:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        fetched = await client.call_tool("example.news.fetch", {"url": news_uri})
        audio = await client.call_tool("example.news.fetch", {"url": audio_uri})
        artifact_id = _payload(audio)["artifacts"][0]["artifact_id"]
        accepted = await client.call_tool(
            "example.transcribe.transcribe", {"artifact_id": artifact_id}
        )
        job_id = _payload(accepted)["job"]["job_id"]
        status: dict[str, Any] = {}
        for _ in range(400):
            status = _payload(await client.call_tool("module_job_status", {"job_id": job_id}))
            if status["state"] not in {"queued", "running"}:
                break
            await anyio.sleep(0.05)
        bad = await client.call_tool("example.news.fetch", {"url": 5})
        search = await client.call_tool("search", {"query": "anything"})
        return {
            "listed": listed,
            "fetched": fetched,
            "accepted": _payload(accepted),
            "status": status,
            "bad": bad,
            "search": search,
        }

    out = _client(server, scenario)
    listed = out["listed"]
    for name in (
        "example.news.fetch",
        "example.transcribe.transcribe",
        "module_job_status",
        "module_job_cancel",
    ):
        assert name in listed
    assert listed["example.news.fetch"].input_schema["required"] == ["url"]
    assert not out["fetched"].is_error
    assert _payload(out["fetched"])["output"]["rights"]["license"] == "CC-BY-4.0"
    assert out["accepted"]["status"] == "accepted"
    assert out["status"]["state"] == "succeeded"
    assert len(out["status"]["result"]["output"]["segments"]) == 3
    assert out["bad"].is_error
    assert not out["search"].is_error


def test_mcp_advertised_schemas_are_frozen_for_the_session(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.probe)
    _use_as_default(host)
    server = _server()
    data = json.loads((mod_env.probe / "module.json").read_text())
    data["version"] = "0.2.0"
    (mod_env.probe / "module.json").write_text(json.dumps(data))

    async def scenario(client: Client) -> tuple[Any, Any, list[str]]:
        before = await client.call_tool("test.probe.run", {"mode": "echo"})
        host.register(mod_env.probe)
        host.enable("test.probe")
        after = await client.call_tool("test.probe.run", {"mode": "echo"})
        names = [t.name for t in (await client.list_tools()).tools]
        return before, after, names

    before, after, names = _client(server, scenario)
    assert not before.is_error
    assert after.is_error
    error = _payload(after)["error"]
    assert error["code"] == "unavailable" and error["details"]["restart_required"] is True
    assert "test.probe.run" in names  # the advertised list does not change mid-session


def test_disabled_module_answers_unavailable_over_mcp(
    mod_env: Any, host: Any, helpers: Any
) -> None:
    helpers.enable(host, mod_env.probe)
    _use_as_default(host)
    server = _server()
    host.disable("test.probe")

    async def call(client: Client) -> Any:
        return await client.call_tool("test.probe.run", {"mode": "echo"})

    result = _client(server, call)
    assert result.is_error and _payload(result)["error"]["code"] == "unavailable"


def test_broken_catalog_never_blocks_core_search(
    mod_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    (mod_env.data / "modules.db").write_bytes(b"this is not a database" * 100)
    server = _server()

    async def scenario(client: Client) -> tuple[list[str], Any]:
        names = [t.name for t in (await client.list_tools()).tools]
        return names, await client.call_tool("search", {"query": "x"})

    names, search = _client(server, scenario)
    assert "search" in names and not search.is_error
    assert not any("." in n and n.split(".")[0] == "example" for n in names)
    assert "MCP tool group 'modules' unavailable" in caplog.text


def test_modules_group_can_be_switched_off(
    mod_env: Any, host: Any, helpers: Any, monkeypatch: Any
) -> None:
    helpers.enable(host, mod_env.probe)
    monkeypatch.setenv("KE_MODULES_ENABLED", "0")
    _use_as_default(host)
    server = _server()

    async def names(client: Client) -> list[str]:
        return [t.name for t in (await client.list_tools()).tools]

    assert "test.probe.run" not in _client(server, names)


# ── external MCP transport ───────────────────────────────────────────


def test_external_stdio_mcp_module(mod_env: Any, host: Any) -> None:
    shutil.copytree(MCP_ECHO, mod_env.modules / "mcp-echo")
    host.register(mod_env.modules / "mcp-echo")
    host.enable("test.mcp-echo")
    result = host.invoke("test.mcp-echo", "shout", {"text": "hello"})
    assert result["output"]["text"] == "HELLO"
    probed = host.status("test.mcp-echo", probe=True)
    assert probed["probe"] == {"ok": True}


# ── HTTP ─────────────────────────────────────────────────────────────


def _admin_headers() -> dict[str, str]:
    from knowledge_engine.agent_board import keys

    master = keys.ensure_master_key()
    assert master is not None
    return {"X-Board-Key": master["raw_key"]}


def test_http_admin_surface(mod_env: Any, helpers: Any) -> None:
    from knowledge_engine.app import create_app

    headers = _admin_headers()
    url = mod_env.fixture_uri("news-fetcher", "solar-storage.html")
    with TestClient(create_app(), client=LOOPBACK, headers=headers) as client:
        found = client.get("/modules/discover")
        assert found.status_code == 200, found.text
        assert {d["directory"] for d in found.json()["directories"]} >= {
            "news-fetcher",
            "transcription",
        }
        r = client.post("/modules/register", json={"directory": "news-fetcher"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "registered"
        r = client.post("/modules/register", json={"directory": "../news-fetcher"})
        assert r.status_code == 400 and r.json()["type"] == "invalid_input"
        r = client.put(
            "/modules/example.news/configuration", json={"settings": {"allow_live_http": False}}
        )
        assert r.status_code == 200, r.text
        r = client.post("/modules/example.news/tools/fetch/invoke", json={"input": {"url": url}})
        assert r.status_code == 503 and r.json()["type"] == "unavailable"
        r = client.post("/modules/example.news/enable", json={"grants": ["read", "process"]})
        assert r.status_code == 200, r.text
        assert r.json()["grants"] == ["process", "read"]
        r = client.post("/modules/example.news/tools/fetch/invoke", json={"input": {"url": url}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["output"]["canonical_url"].startswith("https://news.example.org/")
        r = client.post("/modules/example.news/tools/fetch/invoke", json={"input": {}})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_input"
        ingest = {"input": {"url": url}, "scope": {"workspace_id": "default"}, "ingest": True}
        r = client.post("/modules/example.news/tools/fetch/invoke", json=ingest)
        # Ingestion reaches the evidence store, which knows no workspace yet.
        assert r.status_code == 422 and r.json()["type"] == "unknown_scope", r.text
        client.app.state.knowledge.store.ensure_workspace("default", "Default")
        r = client.post("/modules/example.news/tools/fetch/invoke", json=ingest)
        assert r.status_code == 200, r.text
        receipt = r.json()["ingestion"]
        assert receipt["source_id"] == "module:example.news"
        assert (receipt["created"], receipt["reused"]) == (1, 0)
        (revision_id,) = receipt["artifact_revisions"].values()
        evidence = client.app.state.knowledge.evidence
        stored = evidence.get_revision(revision_id)
        assert stored is not None
        status = client.get("/modules/example.news").json()
        assert status["recent_runs"][0]["surface"] == "http"
        assert status["recent_runs"][0]["principal"].startswith("key:")
        assert client.post("/modules/example.news/health").json()["probe"] == {"ok": True}
        assert client.get("/modules").json()["modules"][0]["module_id"] == "example.news"
        r = client.post("/modules/example.news/disable")
        assert r.status_code == 200 and r.json()["state"] == "disabled"
        r = client.get("/modules/jobs/job_" + "0" * 32)
        assert r.status_code == 422 and r.json()["type"] == "unknown_scope"
        assert client.get("/modules/jobs").json() == {"jobs": []}


def test_http_long_running_job(mod_env: Any, helpers: Any) -> None:
    from knowledge_engine.app import create_app

    with TestClient(create_app(), client=LOOPBACK, headers=_admin_headers()) as client:
        registered = client.post("/modules/register", json={"directory": "probe"})
        assert registered.status_code == 200
        enabled = client.post("/modules/test.probe/enable")
        assert enabled.status_code == 200
        r = client.post(
            "/modules/test.probe/tools/slow_job/invoke",
            json={"input": {"mode": "sleep", "seconds": 60}},
        )
        assert r.status_code == 200, r.text
        job_id = r.json()["job"]["job_id"]
        cancelled = client.post(f"/modules/jobs/{job_id}/cancel").json()
        assert cancelled["state"] == "cancelled"
        assert client.get(f"/modules/jobs/{job_id}").json()["state"] == "cancelled"


def test_http_requires_an_admin_key_and_a_trusted_peer(mod_env: Any) -> None:
    from knowledge_engine.agent_board import keys
    from knowledge_engine.app import create_app

    writer = keys.create_key("writer")
    keys.grant_permission(writer["key_id"], "board", "*", "write")
    with TestClient(create_app(), client=LOOPBACK) as client:
        assert client.get("/modules").status_code == 401
        r = client.post("/modules/register", json={"bogus": True})
        assert r.status_code == 401  # the gate runs before body validation
        assert client.get("/modules", headers={"X-Board-Key": writer["raw_key"]}).status_code == 403
    with TestClient(create_app(), client=INTERNET, headers=_admin_headers()) as client:
        assert client.get("/modules").status_code == 403


def test_http_routes_can_be_switched_off(mod_env: Any, monkeypatch: Any) -> None:
    from knowledge_engine.app import create_app

    monkeypatch.setenv("KE_MODULES_ENABLED", "0")
    with TestClient(create_app(), client=LOOPBACK, headers=_admin_headers()) as client:
        assert client.get("/modules").status_code == 404
        assert client.get("/health").status_code == 200


# ── CLI ──────────────────────────────────────────────────────────────


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    from knowledge_engine.cli import main

    code = main(list(argv))
    captured = capsys.readouterr()
    try:
        payload = json.loads(captured.out) if captured.out.strip() else None
    except ValueError:
        payload = captured.out
    return code, payload, captured.err


def test_cli_end_to_end(mod_env: Any, capsys: pytest.CaptureFixture[str]) -> None:
    code, found, _ = _cli(capsys, "module", "discover")
    assert code == 0 and {d["directory"] for d in found["directories"]} >= {"news-fetcher"}
    for path in (mod_env.news, mod_env.transcription):
        code, out, _ = _cli(capsys, "module", "register", str(path))
        assert code == 0 and out["created"] is True
    code, out, _ = _cli(
        capsys,
        "module",
        "configure",
        "example.news",
        "--set",
        "fixture_roots=fixtures,../transcription/fixtures",
    )
    assert code == 0 and out["settings"]["fixture_roots"] == [
        "fixtures",
        "../transcription/fixtures",
    ]
    assert _cli(capsys, "module", "enable", "example.news")[0] == 0
    assert (
        _cli(
            capsys,
            "module",
            "enable",
            "example.transcribe",
            "--grant",
            "read",
            "--grant",
            "process",
        )[0]
        == 0
    )
    url = mod_env.fixture_uri("transcription", "interview.txt")
    code, fetched, _ = _cli(
        capsys, "module", "invoke", "example.news", "fetch", "--input", json.dumps({"url": url})
    )
    assert code == 0 and fetched["status"] == "ok"
    artifact_id = fetched["artifacts"][0]["artifact_id"]
    code, job, _ = _cli(
        capsys,
        "module",
        "invoke",
        "example.transcribe",
        "transcribe",
        "--input",
        json.dumps({"artifact_id": artifact_id}),
    )
    assert code == 0 and job["state"] == "succeeded", job
    code, status, _ = _cli(capsys, "module", "job", "status", job["job_id"])
    assert code == 0 and status["state"] == "succeeded"
    code, listed, _ = _cli(capsys, "module", "list")
    assert code == 0 and [m["module_id"] for m in listed["modules"]] == [
        "example.news",
        "example.transcribe",
    ]
    code, status, _ = _cli(capsys, "module", "status", "example.news", "--probe")
    assert code == 0 and status["probe"] == {"ok": True}
    assert _cli(capsys, "module", "disable", "example.news")[0] == 0


def test_cli_typed_failures(mod_env: Any, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _cli(capsys, "module", "invoke", "example.news", "fetch", "--input", "{}")
    assert code == 2 and "error[invalid_input]" in err
    _cli(capsys, "module", "register", str(mod_env.news))
    code, _, err = _cli(
        capsys, "module", "invoke", "example.news", "fetch", "--input", '{"url": "x"}'
    )
    assert code == 4 and "error[unavailable]" in err
    code, _, err = _cli(capsys, "module", "enable", "example.news", "--grant", "read")
    assert code == 3 and "error[denied]" in err
    code, _, err = _cli(capsys, "module", "register", str(mod_env.corpus))
    assert code == 3 and "error[denied]" in err
    code, _, err = _cli(capsys, "module", "configure", "example.news", "--set", "nonsense")
    assert code == 2 and "NAME=VALUE" in err
