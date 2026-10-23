"""MCP server on the official SDK: protocol, tool surface, errors and transports.

Everything runs in-process through the SDK's own client (``mcp.Client``) except
the import side-effect check, which needs a fresh interpreter.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import mcp.types
import pytest
from mcp import Client
from mcp.types.version import LATEST_HANDSHAKE_VERSION

# Private SDK module used for one handshake variant; an SDK patch release that
# moves it skips that case instead of turning the whole gate red.
InMemoryTransport = pytest.importorskip("mcp.client._memory").InMemoryTransport

SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_tools.json"
BASE_TOOLS = ["search", "registry_list", "registry_toggle", "registry_get"]
HTTP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 -- the bind address under test, never a listener


@pytest.fixture
def ke_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the engine (and its optional subsystems) at a throwaway tree."""
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    corpus.mkdir()
    data.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    # A missing config file means project-context defaults: its MCP tools are on.
    monkeypatch.setenv("KE_CONFIG_PATH", str(tmp_path / "absent-knowledge-engine.toml"))
    for var in ("KE_MCP_HTTP", "KE_MCP_ALLOWED_HOSTS", "KE_BIND_HOST", "KE_LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    # Project-context tools resolve their root from the CWD; keep it out of the repo.
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _engine() -> tuple[Any, Any, Any]:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    return config, registry, Indexer(config, registry)


def _build(*, include_groups: bool = True) -> Any:
    from knowledge_engine.mcp.server import build_server

    return build_server(*_engine(), include_groups=include_groups)


def _with_client(target: Any, fn: Callable[[Client], Awaitable[Any]], **kwargs: Any) -> Any:
    async def main() -> Any:
        async with Client(target, **kwargs) as client:
            return await fn(client)

    return anyio.run(main)


def _text(result: Any) -> str:
    assert len(result.content) == 1
    return str(result.content[0].text)


def _normalize_generated(schema: Any) -> Any:
    """Strip pydantic's decoration so a generated schema compares with a hand-written one.

    Drops ``title`` keywords and folds ``anyOf: [X, {"type": "null"}]`` (with
    ``default: null``) back into ``X`` — how an optional parameter is spelled
    when generated from ``T | None = None``.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key == "properties":
            out[key] = {name: _normalize_generated(sub) for name, sub in value.items()}
        elif isinstance(value, list):
            out[key] = [_normalize_generated(item) for item in value]
        else:
            out[key] = _normalize_generated(value)
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        inner = next(item for item in any_of if item != {"type": "null"})
        del out["anyOf"]
        if "default" in out and out["default"] is None:
            del out["default"]
        out = {**inner, **out}
    return out


# ── Protocol ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("connect", "expected_version"),
    [
        pytest.param(lambda server: server, mcp.types.LATEST_PROTOCOL_VERSION, id="in-process"),
        pytest.param(InMemoryTransport, mcp.types.LATEST_PROTOCOL_VERSION, id="json-rpc-discover"),
    ],
)
def test_modern_connection_negotiates_latest_protocol(
    ke_env: Path, connect: Callable[[Any], Any], expected_version: str
) -> None:
    from knowledge_engine import __version__
    from knowledge_engine.mcp.server import INSTRUCTIONS

    server = _build(include_groups=False)

    async def handshake(client: Client) -> tuple[str, Any, str | None]:
        return client.protocol_version, client.server_info, client.instructions

    version, info, instructions = _with_client(connect(server), handshake)
    assert version == expected_version == "2026-07-28"
    assert info is not None
    assert (info.name, info.version) == ("knowledge-engine", __version__)
    assert instructions == INSTRUCTIONS


def test_initialize_handshake_still_supported(ke_env: Path) -> None:
    server = _build(include_groups=False)

    async def handshake(client: Client) -> tuple[str, list[str]]:
        tools = await client.list_tools()
        return client.protocol_version, [tool.name for tool in tools.tools]

    version, names = _with_client(server, handshake, mode="legacy")
    assert version == LATEST_HANDSHAKE_VERSION
    assert names == BASE_TOOLS


# ── Tool surface ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_tools_list_matches_pre_migration_snapshot(ke_env: Path, mode: str) -> None:
    """Names, descriptions and inputSchemas are unchanged by the SDK migration."""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    server = _build()

    async def list_tools(client: Client) -> list[dict[str, Any]]:
        result = await client.list_tools()
        return [
            tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in result.tools
        ]

    listed = _with_client(server, list_tools, mode=mode)
    expected_order = [
        tool["name"]
        for group in ("base", "project_context", "agent_board")
        for tool in snapshot[group]
    ]
    assert [tool["name"] for tool in listed] == expected_order
    by_name = {tool["name"]: tool for tool in listed}

    # Legacy groups are re-registered verbatim.
    for legacy in snapshot["project_context"] + snapshot["agent_board"]:
        tool = by_name[legacy["name"]]
        assert tool["description"] == legacy["description"]
        assert tool["inputSchema"] == legacy["inputSchema"]

    # Base tools are typed functions now: same parameters, types, enums, defaults.
    for legacy in snapshot["base"]:
        tool = by_name[legacy["name"]]
        assert tool["description"] == legacy["description"]
        assert _normalize_generated(tool["inputSchema"]) == legacy["inputSchema"]


def test_kind_enums_include_kits(ke_env: Path) -> None:
    server = _build(include_groups=False)

    async def schemas(client: Client) -> dict[str, Any]:
        result = await client.list_tools()
        return {tool.name: tool.input_schema for tool in result.tools}

    by_name = _with_client(server, schemas)
    for tool in ("search", "registry_list"):
        kind = _normalize_generated(by_name[tool])["properties"]["kind"]
        assert kind["enum"] == ["library", "skill", "kit", "tool"]


def test_search_applies_kind_before_limit(ke_env: Path) -> None:
    from knowledge_engine.core.registry import RegistryEntry

    corpus = ke_env / "corpus"
    config, registry, indexer = _engine()
    for kind, prefix, text in (
        ("library", "libraries", "fox fox fox fox"),
        ("skill", "skills", "a fox among many other unrelated words in a longer note"),
    ):
        for n in range(3):
            folder = corpus / prefix / f"{kind}-{n}"
            folder.mkdir(parents=True)
            (folder / "note.md").write_text(text, encoding="utf-8")
            registry.upsert(
                RegistryEntry(
                    id=f"{kind}-{n}", kind=kind, name=f"{kind} {n}", path=f"{prefix}/{kind}-{n}"
                )
            )
    assert indexer.rebuild()["files"] == 6
    from knowledge_engine.mcp.server import build_server

    server = build_server(config, registry, indexer, include_groups=False)

    async def searches(client: Client) -> dict[str, list[dict[str, Any]]]:
        out = {}
        for label, args in (
            ("any", {"query": "fox", "limit": 2}),
            ("library", {"query": "fox", "limit": 2, "kind": "library"}),
            ("skill", {"query": "fox", "limit": 2, "kind": "skill"}),
        ):
            result = await client.call_tool("search", args)
            assert not result.is_error, _text(result)
            out[label] = json.loads(_text(result))
        return out

    hits = _with_client(server, searches)
    # Precondition: unfiltered, libraries outrank skills, so filtering after the
    # LIMIT would leave no skills at all.
    assert {hit["entry_kind"] for hit in hits["any"]} == {"library"}
    assert len(hits["library"]) == 2
    assert {hit["entry_kind"] for hit in hits["library"]} == {"library"}
    assert len(hits["skill"]) == 2
    assert {hit["entry_kind"] for hit in hits["skill"]} == {"skill"}


def test_registry_tools_keep_their_json_payloads(ke_env: Path) -> None:
    from knowledge_engine.core.registry import RegistryEntry

    config, registry, indexer = _engine()
    registry.upsert(RegistryEntry(id="kit-x", kind="kit", name="Kit X", path="kits/x"))
    from knowledge_engine.mcp.server import build_server

    server = build_server(config, registry, indexer, include_groups=False)
    expected_listing = json.dumps(registry.list(kind="kit"), indent=2)

    async def calls(client: Client) -> list[str]:
        texts = []
        for name, args in (
            ("registry_list", {"kind": "kit"}),
            ("registry_get", {"entry_id": "kit-x"}),
            ("registry_get", {"entry_id": "missing"}),
            ("registry_toggle", {"entry_id": "kit-x", "enabled": False}),
            ("registry_toggle", {"entry_id": "missing", "enabled": True}),
        ):
            result = await client.call_tool(name, args)
            assert not result.is_error, _text(result)
            texts.append(_text(result))
        return texts

    listed, got, missing, toggled, toggled_missing = _with_client(server, calls)
    assert listed == expected_listing
    assert json.loads(got)["id"] == "kit-x"
    assert missing == "null"
    assert json.loads(toggled)["updated"] is True
    assert json.loads(toggled)["entry"]["enabled"] is False
    assert json.loads(toggled_missing) == {"updated": False, "entry": None}


# ── Errors ───────────────────────────────────────────────────────────


def test_bad_arguments_are_error_results_and_server_keeps_serving(ke_env: Path) -> None:
    server = _build()

    async def calls(client: Client) -> list[Any]:
        return [
            await client.call_tool("search", {}),
            await client.call_tool("search", {"query": "x", "kind": "bogus"}),
            await client.call_tool("search", {"query": "x", "limit": 0}),
            await client.call_tool("registry_toggle", {"entry_id": "x"}),
            await client.call_tool("no_such_tool", {}),
            await client.call_tool("project_context.search", {"query": 5}),
            await client.call_tool("registry_list", {}),
        ]

    *failures, healthy = _with_client(server, calls)
    assert [result.is_error for result in failures] == [True] * 6
    texts = [_text(result) for result in failures]
    assert "query" in texts[0]
    assert "kind" in texts[1]
    assert "limit must be a positive integer" in texts[2]
    assert "enabled" in texts[3]
    assert "no_such_tool" in texts[4]
    assert "invalid argument 'query'" in texts[5]
    assert not healthy.is_error
    assert json.loads(_text(healthy)) == []


def _probe_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "probe",
            "description": "Test tool.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "flag": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        }
    ]


def test_crashing_tools_return_error_results(
    ke_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.mcp.toolkit import register_group

    config, registry, indexer = _engine()
    from knowledge_engine.mcp.server import build_server

    server = build_server(config, registry, indexer, include_groups=False)

    def explode(name: str, args: dict[str, Any], ctx: Any) -> Any:
        if args.get("note") == "value":
            raise ValueError("note is not acceptable")
        raise RuntimeError("secret internal detail")

    register_group(server, _probe_schemas(), {"probe": explode}, group="test")

    def broken_search(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(indexer, "search", broken_search)

    async def calls(client: Client) -> list[Any]:
        return [
            await client.call_tool("probe", {}),
            await client.call_tool("probe", {"note": "value"}),
            await client.call_tool("search", {"query": "x"}),
            await client.call_tool("registry_list", {}),
        ]

    with caplog.at_level(logging.ERROR, logger="knowledge_engine.mcp.toolkit"):
        crash, anticipated, base_crash, healthy = _with_client(server, calls)

    assert crash.is_error
    assert "internal error: RuntimeError" in _text(crash)
    assert "secret" not in _text(crash)
    assert anticipated.is_error
    assert "note is not acceptable" in _text(anticipated)
    assert base_crash.is_error
    assert "internal error: RuntimeError" in _text(base_crash)
    assert not healthy.is_error
    crashes = [r for r in caplog.records if r.name == "knowledge_engine.mcp.toolkit"]
    assert {r.levelno for r in crashes} == {logging.ERROR}
    assert all(r.exc_info for r in crashes)
    messages = [r.getMessage() for r in crashes]
    assert any("'probe'" in m for m in messages)
    assert any("'search'" in m for m in messages)


def test_group_tools_validate_and_decode_arguments(ke_env: Path) -> None:
    from knowledge_engine.mcp.toolkit import register_group

    server = _build(include_groups=False)
    seen: list[tuple[str, dict[str, Any], Any]] = []
    context = object()

    def record(name: str, args: dict[str, Any], ctx: Any) -> Any:
        seen.append((name, args, ctx))
        if args.get("note") == "malformed":
            return {"not": "an envelope"}
        return {"content": [{"type": "text", "text": json.dumps(args)}]}

    names = register_group(
        server, _probe_schemas(), {"probe": record}, group="test", context=context
    )
    assert names == ["probe"]

    async def calls(client: Client) -> list[Any]:
        return [
            await client.call_tool("probe", {"limit": "5", "tags": '["a", "b"]', "flag": True}),
            await client.call_tool("probe", {"limit": "five"}),
            await client.call_tool("probe", {"note": "malformed"}),
        ]

    decoded, invalid, malformed = _with_client(server, calls)
    assert not decoded.is_error
    assert json.loads(_text(decoded)) == {"limit": 5, "tags": ["a", "b"], "flag": True}
    assert seen[0] == ("probe", {"limit": 5, "tags": ["a", "b"], "flag": True}, context)
    assert invalid.is_error
    assert "invalid argument 'limit'" in _text(invalid)
    assert len(seen) == 2  # the invalid call never reached the handler
    assert malformed.is_error
    assert "malformed tool result" in _text(malformed)


def test_register_group_rejects_duplicates_and_bad_definitions(
    ke_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.mcp.toolkit import register_group

    server = _build(include_groups=False)

    def handler(name: str, args: dict[str, Any], ctx: Any) -> Any:
        return {"content": []}

    def tool(name: str, schema: Any = None) -> dict[str, Any]:
        return {
            "name": name,
            "description": name,
            "inputSchema": schema if schema is not None else {"type": "object"},
        }

    bad_groups: list[tuple[list[dict[str, Any]], dict[str, Any], str]] = [
        ([tool("fresh"), tool("search")], {"fresh": handler, "search": handler}, "duplicate"),
        ([tool("twin"), tool("twin")], {"twin": handler}, "duplicate"),
        ([tool("orphan")], {}, "no dispatch handler"),
        ([{"description": "nameless"}], {}, "without a name"),
        ([tool("weird", {"type": 12})], {"weird": handler}, "invalid inputSchema"),
    ]
    with caplog.at_level(logging.ERROR, logger="knowledge_engine.mcp.toolkit"):
        for schemas, dispatch, message in bad_groups:
            with pytest.raises(ValueError, match=message):
                register_group(server, schemas, dispatch, group="bad")
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == len(bad_groups)

    async def names(client: Client) -> list[str]:
        return [t.name for t in (await client.list_tools()).tools]

    # Nothing from a rejected group is registered, not even its valid tools.
    assert _with_client(server, names) == BASE_TOOLS


def test_group_init_failures_are_logged_and_skipped(
    ke_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.mcp import server as server_mod

    def broken() -> Any:
        raise ImportError("optional dependency missing")

    def disabled() -> Any:
        raise server_mod.GroupDisabledError("disabled by config")

    monkeypatch.setattr(server_mod, "GROUP_LOADERS", (("broken", broken), ("off", disabled)))
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.mcp.server"):
        server = _build()

    async def names(client: Client) -> list[str]:
        return [t.name for t in (await client.list_tools()).tools]

    assert _with_client(server, names) == BASE_TOOLS
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'broken'" in m and "optional dependency missing" in m for m in warnings)
    assert any("'off'" in m and "disabled by config" in m for m in warnings)


def test_project_context_group_respects_config(
    ke_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_file = ke_env / "knowledge-engine.toml"
    config_file.write_text("[project_context.mcp]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setenv("KE_CONFIG_PATH", str(config_file))
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.mcp.server"):
        server = _build()

    async def names(client: Client) -> list[str]:
        return [t.name for t in (await client.list_tools()).tools]

    listed = _with_client(server, names)
    assert listed[:4] == BASE_TOOLS
    assert not any(name.startswith("project_context.") for name in listed)
    assert any(name.startswith("board_") for name in listed)
    assert any("'project_context'" in r.getMessage() for r in caplog.records)


# ── Entry points ─────────────────────────────────────────────────────


def test_selftest_exits_zero(ke_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from knowledge_engine.mcp.server import main

    assert main(["--selftest"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["protocol_version"] == mcp.types.LATEST_PROTOCOL_VERSION
    assert report["tools"][:4] == BASE_TOOLS
    assert report["tool_count"] == len(report["tools"]) > 4


def test_invalid_registry_fails_the_start_cleanly(
    ke_env: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.mcp.server import main

    (ke_env / "corpus" / "registry.json").write_text(
        json.dumps({"version": 1, "libraries": "not-a-list"}), encoding="utf-8"
    )
    with caplog.at_level(logging.ERROR):
        assert main(["--selftest"]) == 1
    assert capsys.readouterr().out == ""  # stdout is the stdio transport
    messages = [r.getMessage() for r in caplog.records]
    assert any("/libraries" in m for m in messages)
    assert any("MCP server not started" in m for m in messages)


def test_cli_mcp_subcommand_accepts_transport_flags(
    ke_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from knowledge_engine import cli

    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "mcp", "--selftest"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "mcp", "--http", "--port", "0"])
    with pytest.raises(SystemExit):
        cli.main()
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "mcp", "--http", "--selftest"])
    with pytest.raises(SystemExit):
        cli.main()


def test_build_server_leaves_host_logging_alone(
    ke_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK constructor would call logging.basicConfig on a bare root logger."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    _build(include_groups=False)
    assert root.handlers == []
    assert root.level == logging.WARNING


def test_import_has_no_side_effects() -> None:
    probe = (
        "import io, os, sys\n"
        "env = dict(os.environ)\n"
        "out, sys.stdout = sys.stdout, io.StringIO()\n"
        "import knowledge_engine.mcp.server\n"
        "written, sys.stdout = sys.stdout.getvalue(), out\n"
        "assert written == '', repr(written)\n"
        "assert dict(os.environ) == env, 'os.environ was mutated'\n"
    )
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_deprecated_shim_warns_and_reexports_main() -> None:
    from knowledge_engine.mcp import server

    with pytest.warns(DeprecationWarning, match="knowledge_engine.mcp_server is deprecated"):
        if "knowledge_engine.mcp_server" in sys.modules:
            shim = importlib.reload(sys.modules["knowledge_engine.mcp_server"])
        else:
            shim = importlib.import_module("knowledge_engine.mcp_server")
    assert shim.main is server.main
    assert not hasattr(shim, "TOOLS")
    assert not hasattr(shim, "Server")


# ── Streamable HTTP mount ────────────────────────────────────────────


def _sse_json(response: Any) -> dict[str, Any]:
    if response.headers["content-type"].startswith("application/json"):
        return dict(response.json())
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return dict(json.loads(line.removeprefix("data:").strip()))
    raise AssertionError(f"no JSON-RPC message in response: {response.text!r}")


@pytest.fixture
def mcp_http_app(ke_env: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("KE_MCP_HTTP", "1")
    monkeypatch.setenv("KE_BOARD_SWEEPER", "0")
    from knowledge_engine.mcp import server as server_mod

    real_build = server_mod.build_server
    built_with: list[tuple[Any, ...]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        built_with.append(args)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(server_mod, "build_server", spy)
    from knowledge_engine.app import create_app

    app = create_app()
    app.state.built_with = built_with
    yield app


def _initialize(client: Any, protocol_version: str) -> tuple[dict[str, Any], dict[str, str]]:
    response = client.post(
        "/mcp",
        headers=HTTP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session = {
        **HTTP_HEADERS,
        "mcp-session-id": response.headers["mcp-session-id"],
        "mcp-protocol-version": protocol_version,
    }
    return _sse_json(response), session


def test_http_mount_serves_mcp(mcp_http_app: Any) -> None:
    from fastapi.testclient import TestClient

    app = mcp_http_app
    # The factory does no I/O: the server is built by the lifespan, around the
    # registry and index that lifespan opened.
    assert app.state.built_with == []
    with TestClient(app, base_url="http://127.0.0.1:9210") as client:
        assert app.state.built_with == [(app.state.config, app.state.registry, app.state.indexer)]
        r = client.post(
            "/registry", json={"id": "kit-x", "kind": "kit", "name": "X", "path": "kits/x"}
        )
        assert r.status_code == 200, r.text

        message, session = _initialize(client, "2024-11-05")
        assert message["result"]["protocolVersion"] == "2024-11-05"
        assert message["result"]["serverInfo"]["name"] == "knowledge-engine"

        r = client.post(
            "/mcp", headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert r.status_code == 202, r.text
        r = client.post(
            "/mcp", headers=session, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        names = [tool["name"] for tool in _sse_json(r)["result"]["tools"]]
        assert names[:4] == BASE_TOOLS
        r = client.post(
            "/mcp",
            headers=session,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "registry_get", "arguments": {"entry_id": "kit-x"}},
            },
        )
        result = _sse_json(r)["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["kind"] == "kit"

        # The modern (2026-07-28) per-request envelope is served on the same endpoint.
        latest = mcp.types.LATEST_PROTOCOL_VERSION
        r = client.post(
            "/mcp",
            headers={
                **HTTP_HEADERS,
                "mcp-protocol-version": latest,
                "mcp-method": "server/discover",
            },
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": latest,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert r.status_code == 200, r.text
        assert latest in _sse_json(r)["result"]["supportedVersions"]

        # DNS-rebinding protection: foreign Host headers are refused.
        r = client.post("/mcp", headers={**HTTP_HEADERS, "Host": "evil.example"}, json={})
        assert r.status_code == 421

    # The session manager is single-use; a second lifespan gets a fresh one
    # (and a fresh server around that lifespan's newly opened registry/index).
    with TestClient(app, base_url="http://127.0.0.1:9210") as client:
        assert len(app.state.built_with) == 2
        assert app.state.built_with[-1][1] is app.state.registry
        message, _ = _initialize(client, LATEST_HANDSHAKE_VERSION)
        assert message["result"]["protocolVersion"] == LATEST_HANDSHAKE_VERSION
    # Outside the lifespan the endpoint answers 503 instead of crashing.
    assert (
        TestClient(app, base_url="http://127.0.0.1:9210").post("/mcp", json={}).status_code == 503
    )


def test_http_allowed_hosts_env(mcp_http_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KE_MCP_ALLOWED_HOSTS", "kb.internal")
    with TestClient(mcp_http_app, base_url="http://kb.internal") as client:
        message, _ = _initialize(client, LATEST_HANDSHAKE_VERSION)
        assert message["result"]["serverInfo"]["name"] == "knowledge-engine"
        r = client.post("/mcp", headers={**HTTP_HEADERS, "Host": "evil.example"}, json={})
        assert r.status_code == 421


def test_http_mount_is_off_by_default(ke_env: Path) -> None:
    from fastapi.testclient import TestClient

    from knowledge_engine.app import create_app

    app = create_app()
    assert not hasattr(app.state, "mcp_http")
    with TestClient(app, base_url="http://127.0.0.1:9210") as client:
        assert client.post("/mcp", headers=HTTP_HEADERS, json={}).status_code == 404


def test_transport_security_policy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.mcp.http import transport_security

    monkeypatch.delenv("KE_MCP_ALLOWED_HOSTS", raising=False)
    # Loopback binds: protection on, loopback names only (the SDK default made explicit).
    for bind in ("127.0.0.1", "localhost", "::1"):
        settings = transport_security(bind)
        assert settings.enable_dns_rebinding_protection
        assert {"127.0.0.1", "127.0.0.1:*", "localhost:*", "[::1]:*"} <= set(settings.allowed_hosts)
        assert not any("kb.internal" in host for host in settings.allowed_hosts)
    # Any other bind without KE_MCP_ALLOWED_HOSTS: the check is off, and operators are told.
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.mcp.http"):
        settings = transport_security(ALL_INTERFACES)
    assert not settings.enable_dns_rebinding_protection
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ALL_INTERFACES in warnings[0] and "KE_MCP_ALLOWED_HOSTS" in warnings[0]

    monkeypatch.setenv("KE_MCP_ALLOWED_HOSTS", " kb.internal , ")
    for bind in ("127.0.0.1", ALL_INTERFACES):
        settings = transport_security(bind)
        assert settings.enable_dns_rebinding_protection
        assert {"kb.internal", "kb.internal:*", "127.0.0.1:*"} <= set(settings.allowed_hosts)
        assert "https://kb.internal:*" in settings.allowed_origins
    monkeypatch.setenv("KE_MCP_ALLOWED_HOSTS", "*")
    settings = transport_security()
    assert not settings.enable_dns_rebinding_protection


def test_http_mount_follows_the_bind_host(
    ke_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`serve --host 0.0.0.0` must not answer 421 to every non-loopback Host (reverse proxies)."""
    from fastapi.testclient import TestClient

    from knowledge_engine.app import create_app

    monkeypatch.setenv("KE_MCP_HTTP", "1")
    monkeypatch.setenv("KE_BOARD_SWEEPER", "0")
    monkeypatch.setenv("KE_BIND_HOST", ALL_INTERFACES)
    app = create_app()
    assert app.state.mcp_http.host == ALL_INTERFACES
    with (
        caplog.at_level(logging.WARNING, logger="knowledge_engine.mcp.http"),
        TestClient(app, base_url="http://kb.internal:9210") as client,
    ):
        message, _ = _initialize(client, LATEST_HANDSHAKE_VERSION)
        assert message["result"]["serverInfo"]["name"] == "knowledge-engine"
        r = client.post("/mcp", headers={**HTTP_HEADERS, "Host": "192.168.1.20:9210"}, json={})
        assert r.status_code != 421, r.text
    assert any("KE_MCP_ALLOWED_HOSTS" in r.getMessage() for r in caplog.records)

    # An allowlist restores the check on a non-loopback bind.
    monkeypatch.setenv("KE_MCP_ALLOWED_HOSTS", "kb.internal")
    with TestClient(app, base_url="http://kb.internal:9210") as client:
        message, _ = _initialize(client, LATEST_HANDSHAKE_VERSION)
        assert message["result"]["serverInfo"]["name"] == "knowledge-engine"
        r = client.post("/mcp", headers={**HTTP_HEADERS, "Host": "192.168.1.20:9210"}, json={})
        assert r.status_code == 421


def test_cli_serve_exports_the_bind_host(ke_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from knowledge_engine import cli

    calls: list[dict[str, Any]] = []

    def fake_run(app: str, **kwargs: Any) -> None:
        calls.append({"app": app, "bind_host_env": os.environ.get("KE_BIND_HOST"), **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv", ["knowledge-engine", "serve", "--host", ALL_INTERFACES, "--port", "1"]
    )
    assert cli.main() == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["app"] == "knowledge_engine.app:create_app"
    assert call["bind_host_env"] == ALL_INTERFACES
    assert (call["host"], call["port"], call["factory"]) == (ALL_INTERFACES, 1, True)
    # `serve` also wires logging and proxy handling (see core.http / core.logging_config).
    assert "log_config" in call and "proxy_headers" in call


@pytest.mark.parametrize("flags", [["--host", "0.0.0.0"], ["--port", "9300"]])  # noqa: S104
def test_host_and_port_require_http(flags: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    from knowledge_engine.mcp.server import main

    with pytest.raises(SystemExit) as exc:
        main(["--selftest", *flags])
    assert exc.value.code == 2
    assert "--http" in capsys.readouterr().err


def test_http_defaults_fill_in_when_flags_are_omitted() -> None:
    import argparse

    from knowledge_engine.mcp.options import (
        DEFAULT_HTTP_HOST,
        DEFAULT_HTTP_PORT,
        add_arguments,
        resolve_http_options,
    )

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(["--http"])
    resolve_http_options(parser, args)
    assert (args.host, args.port) == (DEFAULT_HTTP_HOST, DEFAULT_HTTP_PORT)
