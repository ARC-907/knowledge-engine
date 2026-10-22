"""MCP `search` tool: the `kind` filter applies before `limit`."""

from __future__ import annotations

import json
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
