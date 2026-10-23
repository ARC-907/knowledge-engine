"""MCP `search` tool: the `kind` filter applies before `limit`, bad input is rejected."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.core.registry import RegistryEntry


def _write(folder: Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A Server over a corpus where libraries out-rank skills for "fox"."""
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

    from knowledge_engine.mcp.server import Server

    srv = Server()
    srv.registry.upsert(RegistryEntry(id="zoo", kind="library", name="Zoo", path="libraries/zoo"))
    srv.registry.upsert(
        RegistryEntry(id="tracking", kind="skill", name="Tracking", path="skills/tracking")
    )
    assert srv.indexer.rebuild()["files"] == 8
    return srv


def _search(srv: Any, **arguments: Any) -> list[dict[str, Any]]:
    resp = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search", "arguments": arguments},
        }
    )
    assert "error" not in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_kind_filter_applies_before_limit(server: Any) -> None:
    unfiltered = _search(server, query="fox", limit=2)
    assert [r["entry_kind"] for r in unfiltered] == ["library", "library"]

    skills = _search(server, query="fox", limit=2, kind="skill")

    assert len(skills) == 2
    assert {r["entry_kind"] for r in skills} == {"skill"}


def test_kind_filter_returns_all_matches_below_limit(server: Any) -> None:
    skills = _search(server, query="fox", limit=10, kind="skill")
    assert len(skills) == 3
    assert {r["entry_id"] for r in skills} == {"tracking"}


def test_no_kind_and_unknown_kind(server: Any) -> None:
    assert len(_search(server, query="fox", limit=10)) == 8
    assert _search(server, query="fox", kind="") == _search(server, query="fox")
    assert _search(server, query="fox", kind="kit") == []


def test_indexer_search_kind_keyword(server: Any) -> None:
    hits = server.indexer.search("fox", limit=1, kind="skill")
    assert [h["entry_kind"] for h in hits] == ["skill"]


def _call(srv: Any, **arguments: Any) -> dict[str, Any]:
    resp = srv.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "search", "arguments": arguments},
        }
    )
    assert resp is not None
    return resp


@pytest.mark.parametrize("kind", [["library"], {"k": "library"}, 3])
def test_non_string_kind_is_an_invalid_params_error(server: Any, kind: Any) -> None:
    resp = _call(server, query="fox", kind=kind)
    assert resp["id"] == 7
    assert resp["error"]["code"] == -32602
    assert "kind" in resp["error"]["message"]


def test_non_string_query_is_an_invalid_params_error(server: Any) -> None:
    resp = _call(server, query=["fox"])
    assert resp["error"]["code"] == -32602
    assert "query" in resp["error"]["message"]


def test_server_survives_a_bad_kind_then_answers_the_next_call(server: Any) -> None:
    _call(server, query="fox", kind=["library"])
    assert len(_search(server, query="fox", kind="library")) == 5


def test_indexer_rejects_non_string_kind_at_the_sqlite_layer(server: Any) -> None:
    # Documents why the server must validate first: the indexer binds `kind`
    # straight into SQL and sqlite3 refuses non-scalar parameters.
    with pytest.raises(sqlite3.ProgrammingError):
        server.indexer.search("fox", kind=["library"])  # type: ignore[arg-type]


def test_main_turns_unexpected_exceptions_into_internal_errors(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "Server", lambda: server)

    def boom(_msg: dict[str, Any]) -> dict[str, Any] | None:
        raise sqlite3.ProgrammingError("Error binding parameter 2")

    monkeypatch.setattr(server, "handle", boom)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}) + "\n"
            "not json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
        ),
    )
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)

    assert mcp_server.main() == 0

    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    assert all(r["error"]["code"] == -32603 for r in responses)
    assert all(r["error"]["message"] == "internal error" for r in responses)
