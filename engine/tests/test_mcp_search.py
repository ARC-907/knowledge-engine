"""MCP `search` tool: the `kind` filter applies before `limit`, bad input is rejected.

Calls go through the SDK's in-process client (``mcp.Client``) against a server
from :func:`knowledge_engine.mcp.server.build_server`. Since the SDK migration,
bad arguments come back as a tool result with ``isError`` instead of a JSON-RPC
``-32602`` error; the intent is unchanged: the server reports the problem and
keeps answering.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from knowledge_engine.core.registry import RegistryEntry


def _write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


@dataclass
class Engine:
    """The MCP server plus the engine objects it was built around."""

    server: Any
    registry: Any
    indexer: Any


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A server over a corpus where libraries out-rank skills for "fox"."""
    corpus = tmp_path / "corpus"
    # Five short, fox-dense library notes: these take every top-ranked slot.
    for i in range(5):
        _write(corpus / "libraries" / "zoo", f"note{i}.md", "fox fox fox")
    # Three long skill notes mention "fox" once: they rank below every library hit.
    filler = " ".join(f"word{n}" for n in range(200))
    for i in range(3):
        _write(corpus / "skills" / "tracking", f"skill{i}.md", f"fox {filler}")

    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))

    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.mcp.server import build_server
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    indexer = Indexer(config, registry)
    registry.upsert(RegistryEntry(id="zoo", kind="library", name="Zoo", path="libraries/zoo"))
    registry.upsert(
        RegistryEntry(id="tracking", kind="skill", name="Tracking", path="skills/tracking")
    )
    assert indexer.rebuild()["files"] == 8
    return Engine(build_server(config, registry, indexer, include_groups=False), registry, indexer)


def _with_client(engine: Engine, fn: Callable[[Client], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        async with Client(engine.server) as client:
            return await fn(client)

    return anyio.run(main)


def _text(result: Any) -> str:
    assert len(result.content) == 1
    return str(result.content[0].text)


def _call(engine: Engine, **arguments: Any) -> Any:
    """One `search` call; returns the SDK's ``CallToolResult``."""

    async def call(client: Client) -> Any:
        return await client.call_tool("search", arguments)

    return _with_client(engine, call)


def _search(engine: Engine, **arguments: Any) -> list[dict[str, Any]]:
    result = _call(engine, **arguments)
    assert not result.is_error, _text(result)
    return list(json.loads(_text(result)))


def test_kind_filter_applies_before_limit(server: Engine) -> None:
    unfiltered = _search(server, query="fox", limit=2)
    assert [r["entry_kind"] for r in unfiltered] == ["library", "library"]

    skills = _search(server, query="fox", limit=2, kind="skill")

    assert len(skills) == 2
    assert {r["entry_kind"] for r in skills} == {"skill"}


def test_kind_filter_returns_all_matches_below_limit(server: Engine) -> None:
    skills = _search(server, query="fox", limit=10, kind="skill")
    assert len(skills) == 3
    assert {r["entry_id"] for r in skills} == {"tracking"}


def test_no_kind_and_unknown_kind(server: Engine) -> None:
    assert len(_search(server, query="fox", limit=10)) == 8
    assert _search(server, query="fox", kind=None) == _search(server, query="fox")
    assert _search(server, query="fox", kind="kit") == []


def test_empty_kind_means_no_filter(server: Engine) -> None:
    # Pre-SDK contract kept: "" is "no filter", while a wrong kind is still rejected.
    assert _search(server, query="fox", kind="") == _search(server, query="fox")
    result = _call(server, query="fox", kind="plugin")
    assert result.is_error
    assert "kind" in _text(result)


def test_indexer_search_kind_keyword(server: Engine) -> None:
    hits = server.indexer.search("fox", limit=1, kind="skill")
    assert [h["entry_kind"] for h in hits] == ["skill"]


@pytest.mark.parametrize("kind", [["library"], {"k": "library"}, 3])
def test_non_string_kind_is_an_error_result(server: Engine, kind: Any) -> None:
    result = _call(server, query="fox", kind=kind)
    assert result.is_error
    assert "kind" in _text(result)


def test_non_string_query_is_an_error_result(server: Engine) -> None:
    result = _call(server, query=["fox"])
    assert result.is_error
    assert "query" in _text(result)


def test_server_survives_a_bad_kind_then_answers_the_next_call(server: Engine) -> None:
    async def calls(client: Client) -> tuple[Any, Any]:
        bad = await client.call_tool("search", {"query": "fox", "kind": ["library"]})
        good = await client.call_tool("search", {"query": "fox", "kind": "library"})
        return bad, good

    bad, good = _with_client(server, calls)
    assert bad.is_error
    assert not good.is_error
    assert len(json.loads(_text(good))) == 5


def test_indexer_rejects_non_string_kind_at_the_sqlite_layer(server: Engine) -> None:
    # Documents why the server must validate first: the indexer binds `kind`
    # straight into SQL and sqlite3 refuses non-scalar parameters.
    with pytest.raises(sqlite3.ProgrammingError):
        server.indexer.search("fox", kind=["library"])  # type: ignore[arg-type]


def test_unexpected_exceptions_become_internal_errors_and_the_session_survives(
    server: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside a tool is reported as an opaque internal error, not a dead server."""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.ProgrammingError("Error binding parameter 2")

    monkeypatch.setattr(server.indexer, "search", boom)

    async def calls(client: Client) -> tuple[Any, list[str], Any]:
        crashed = await client.call_tool("search", {"query": "fox"})
        names = [tool.name for tool in (await client.list_tools()).tools]
        healthy = await client.call_tool("registry_get", {"entry_id": "zoo"})
        return crashed, names, healthy

    crashed, names, healthy = _with_client(server, calls)
    assert crashed.is_error
    assert "internal error: ProgrammingError" in _text(crashed)
    assert "binding parameter" not in _text(crashed)
    assert "search" in names
    assert not healthy.is_error
    assert json.loads(_text(healthy))["id"] == "zoo"


def test_fts5_syntax_fallback_is_flagged_in_meta(server: Engine) -> None:
    good = _call(server, query="fox", limit=1)
    assert not good.is_error
    assert not (good.meta or {}).get("syntax_fallback")

    fallback = _call(server, query='"fox', limit=3)
    assert not fallback.is_error
    assert (fallback.meta or {}).get("syntax_fallback") is True
    hits = json.loads(_text(fallback))
    assert isinstance(hits, list)
    assert all(hit["path"] for hit in hits)
