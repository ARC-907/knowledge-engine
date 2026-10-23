"""Search modes ``fts`` / ``vector`` / ``hybrid``: RRF, HTTP ``/search``, MCP ``search``, CLI.

Offline: the embedding index is built with ``KE_EMBED_PROVIDER=fake``
(deterministic feature-hashed vectors); no Ollama is contacted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from knowledge_engine import cli
from knowledge_engine.core.registry import RegistryEntry
from knowledge_engine.retrieval.hybrid import (
    DEFAULT_RRF_K,
    EntryResolver,
    excerpt,
    parse_mode,
    reciprocal_rank_fusion,
)

FOX = "# Foxes\n\nThe quick red fox jumps over the lazy dog. Foxes are small omnivorous mammals.\n"
OWL = "# Owls\n\nOwls are nocturnal birds of prey with excellent night vision and silent flight.\n"
TRACK = "# Tracking\n\nHow to track a fox in the snow: look for small paw prints in a line.\n"
HIDDEN = "# Hidden\n\nA disabled library that also talks about the red fox and its den.\n"


# ── reciprocal rank fusion ───────────────────────────────────────────


def test_rrf_scores_and_orders_known_ranks() -> None:
    fts = ["a", "b", "c"]
    vector = ["c", "a", "d"]
    fused = reciprocal_rank_fusion([fts, vector], k=60)
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores["d"] == pytest.approx(1 / 63)
    # a (ranks 1, 2) > c (ranks 3, 1) > b (rank 2 only) > d (rank 3 only)
    assert [key for key, _ in fused] == ["a", "c", "b", "d"]


def test_rrf_ties_keep_first_appearance_and_k_matters() -> None:
    # x and y each sit at rank 1 in one list: equal scores, x was seen first.
    assert [key for key, _ in reciprocal_rank_fusion([["x"], ["y"]])] == ["x", "y"]
    # Duplicates inside one ranking count once, at their best rank.
    assert dict(reciprocal_rank_fusion([["x", "x"]]))["x"] == pytest.approx(1 / 61)
    # k=0: rank 1 is worth 1.0, so one top rank beats two middling ones.
    fused = reciprocal_rank_fusion([["p", "q"], ["r", "q"]], k=0)
    assert fused[0] == ("p", 1.0)
    assert DEFAULT_RRF_K == 60
    assert reciprocal_rank_fusion([]) == []
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], k=-1)


def test_parse_mode_and_helpers() -> None:
    assert parse_mode(None) == "fts"
    assert parse_mode(" Hybrid ") == "hybrid"
    with pytest.raises(ValueError, match="unknown search mode"):
        parse_mode("semantic")
    assert excerpt("a\n\n b   c") == "a b c"
    assert excerpt("word " * 100, limit=20).endswith("...")
    resolver = EntryResolver(
        [
            {"id": "outer", "kind": "library", "path": "libraries/zoo"},
            {"id": "inner", "kind": "skill", "path": "libraries/zoo/birds/"},
        ]
    )
    assert resolver.resolve("libraries/zoo/birds/owl.md")["id"] == "inner"  # type: ignore[index]
    assert resolver.resolve("libraries/zoo/fox.md")["id"] == "outer"  # type: ignore[index]
    assert resolver.resolve("libraries/zoology/x.md") is None
    only_skills = EntryResolver(
        [{"id": "outer", "kind": "library", "path": "libraries/zoo"}], "skill"
    )
    assert not only_skills.accepts("libraries/zoo/fox.md")


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def engine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A corpus with two enabled entries and one disabled one, FTS + fake embeddings built."""
    corpus = tmp_path / "corpus"
    for rel, text in (
        ("libraries/zoo/fox.md", FOX),
        ("libraries/zoo/owl.md", OWL),
        ("skills/track/t.md", TRACK),
        ("libraries/hidden/h.md", HIDDEN),
    ):
        (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
        (corpus / rel).write_text(text, encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    monkeypatch.setenv("KE_PIPELINE_ROOT", str(tmp_path / "pipeline"))
    monkeypatch.setenv("KE_CONFIG_PATH", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("KE_EMBED_PROVIDER", "fake")
    monkeypatch.chdir(tmp_path)

    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.retrieval.embeddings.index import CorpusEmbeddingIndex
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path)
    registry.upsert(RegistryEntry(id="zoo", kind="library", name="Zoo", path="libraries/zoo"))
    registry.upsert(RegistryEntry(id="track", kind="skill", name="Track", path="skills/track"))
    registry.upsert(
        RegistryEntry(
            id="hidden", kind="library", name="Hidden", path="libraries/hidden", enabled=False
        )
    )
    indexer = Indexer(config, registry)
    indexer.rebuild()
    indexer.close()
    registry.close()
    CorpusEmbeddingIndex.from_settings().build()
    return tmp_path


def _app() -> Any:
    from knowledge_engine.app import create_app

    return create_app()


# ── HTTP ─────────────────────────────────────────────────────────────


def test_http_default_mode_is_fts_and_unchanged(engine_env: Path) -> None:
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "fts"
        assert {hit["path"] for hit in body["results"]} == {
            "libraries/zoo/fox.md",
            "skills/track/t.md",
        }
        for hit in body["results"]:
            assert set(hit) == {"entry_id", "entry_kind", "path", "snippet", "score"}
            assert "<mark>" in hit["snippet"]
        assert client.get("/search", params={"q": "fox", "mode": "fts"}).json() == body


def test_http_hybrid_fuses_both_rankings(engine_env: Path) -> None:
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox", "mode": "hybrid", "limit": 10})
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["mode"] == "hybrid"
    hits = body["results"]
    assert body["count"] == len(hits) == 3  # fox.md, t.md (both lists) + owl.md (vector only)
    paths = [hit["path"] for hit in hits]
    assert "libraries/hidden/h.md" not in paths  # disabled entries stay out
    fused = [hit["fused_score"] for hit in hits]
    assert fused == sorted(fused, reverse=True)
    for hit in hits:
        assert hit["score"] == pytest.approx(-hit["fused_score"])
        assert hit["entry_id"] in {"zoo", "track"}
    both = [hit for hit in hits if "fts_score" in hit]
    assert {hit["path"] for hit in both} == {"libraries/zoo/fox.md", "skills/track/t.md"}
    assert all("vector_score" in hit for hit in hits)
    vector_only = next(hit for hit in hits if hit["path"] == "libraries/zoo/owl.md")
    assert "<mark>" not in vector_only["snippet"]
    assert vector_only["fused_score"] < min(hit["fused_score"] for hit in both)


def test_http_vector_mode_and_kind_filter(engine_env: Path) -> None:
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox paw prints snow", "mode": "vector"})
        body = r.json()
        assert r.status_code == 200, r.text
        assert body["mode"] == "vector"
        hits = body["results"]
        assert hits[0]["path"] == "skills/track/t.md"
        assert hits[0]["entry_kind"] == "skill"
        assert hits[0]["score"] == pytest.approx(-hits[0]["vector_score"])
        assert "fused_score" not in hits[0]
        r = client.get("/search", params={"q": "nocturnal", "mode": "invalid"})
        assert r.status_code == 422


def test_http_vector_errors_are_structured(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Index built with the fake model; the server now embeds queries with bge-m3.
    monkeypatch.setenv("KE_EMBED_PROVIDER", "ollama")
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox", "mode": "hybrid"})
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["type"] == "embedding_model_mismatch"
        assert "--rebuild" in body["hint"]
        assert body["embedding"]["index_model"] == "fake-hash-64"
        assert body["embedding"]["model"] == "bge-m3:latest"
        # The index was built by another provider: the hint says how to switch back.
        assert "KE_EMBED_PROVIDER=fake KE_EMBED_MODEL=fake-hash-64" in body["hint"]
        # HTTP clients do not learn where the index lives on disk.
        assert "db_path" not in body["embedding"]
        assert str(engine_env) not in r.text
        # fts keeps working.
        assert client.get("/search", params={"q": "fox"}).status_code == 200

    (engine_env / "data" / "embeddings.db").unlink()
    monkeypatch.setenv("KE_EMBED_PROVIDER", "fake")
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox", "mode": "vector"})
        assert r.status_code == 503
        assert r.json()["type"] == "embedding_index_missing"

    monkeypatch.setenv("KE_EMBED_PROVIDER", "remote")  # not allowed: vector search disabled
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox", "mode": "vector"})
        assert r.status_code == 503
        assert r.json()["type"] == "embedding_unavailable"
        assert "KE_EMBED_ALLOW_REMOTE" in r.json()["detail"]
        assert client.get("/search", params={"q": "fox"}).status_code == 200


# ── MCP ──────────────────────────────────────────────────────────────


def _mcp(fn: Callable[[Client], Awaitable[Any]]) -> Any:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.mcp.server import build_server
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    registry = Registry(config.registry_path)
    indexer = Indexer(config, registry)
    server = build_server(config, registry, indexer, include_groups=False)

    async def main() -> Any:
        async with Client(server) as client:
            return await fn(client)

    try:
        return anyio.run(main)
    finally:
        indexer.close()


def test_mcp_search_mode(engine_env: Path) -> None:
    async def calls(client: Client) -> dict[str, Any]:
        return {
            mode: await client.call_tool(
                "search", {"query": "fox", **({"mode": mode} if mode else {})}
            )
            for mode in ("", "fts", "vector", "hybrid", "bogus")
        }

    results = _mcp(calls)
    default, fts = results[""], results["fts"]
    assert not default.is_error
    assert json.loads(default.content[0].text) == json.loads(fts.content[0].text)
    assert "mode" not in (default.meta or {})  # plain fts: no mode flag, exactly as before

    hybrid = results["hybrid"]
    assert not hybrid.is_error
    hits = json.loads(hybrid.content[0].text)
    assert {hit["path"] for hit in hits} == {
        "libraries/zoo/fox.md",
        "skills/track/t.md",
        "libraries/zoo/owl.md",
    }
    assert all("fused_score" in hit for hit in hits)
    assert (hybrid.meta or {}).get("mode") == "hybrid"
    vector = results["vector"]
    assert not vector.is_error
    assert (vector.meta or {}).get("mode") == "vector"
    assert results["bogus"].is_error


def test_mcp_search_mode_error_is_structured(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KE_EMBED_PROVIDER", "ollama")

    async def call(client: Client) -> Any:
        return await client.call_tool("search", {"query": "fox", "mode": "vector"})

    result = _mcp(call)
    assert result.is_error
    error = json.loads(result.content[0].text)["error"]
    assert error["type"] == "embedding_model_mismatch"
    assert error["embedding"]["index_model"] == "fake-hash-64"


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_embeddings_commands(engine_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["embeddings", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["built"] is True
    assert status["model"] == "fake-hash-64"
    assert status["files"] == 4

    assert cli.main(["embeddings", "build", "--incremental"]) == 0
    assert json.loads(capsys.readouterr().out)["indexed"] == 0

    assert cli.main(["embeddings", "search", "fox paw prints snow", "--top", "1"]) == 0
    [hit] = json.loads(capsys.readouterr().out)
    assert hit["rel_path"] == "skills/track/t.md"

    assert cli.main(["embeddings", "search", "fox", "--library", "skills"]) == 0
    assert [h["library"] for h in json.loads(capsys.readouterr().out)] == ["skills"]

    assert cli.main(["search", "fox", "--mode", "hybrid", "--limit", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_cli_embeddings_refuses_another_model(
    engine_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["embeddings", "build", "--provider", "ollama", "--model", "nomic"]) == 1
    captured = capsys.readouterr()
    error = json.loads(captured.out)["error"]
    assert error["type"] == "embedding_model_mismatch"
    assert error["embedding"]["index_model"] == "fake-hash-64"
    assert "--rebuild" in captured.err
    assert cli.main(["search", "fox", "--mode", "vector"]) == 0  # env still says fake
    capsys.readouterr()
    assert cli.main(["embeddings", "search", "fox", "--provider", "ollama"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "embedding_model_mismatch"


def test_cli_embeddings_build_to_explicit_db_and_rebuild(
    engine_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = engine_env / "other.db"
    assert (
        cli.main(["embeddings", "build", "--db", str(db), "--batch", "2", "--chunk-size", "800"])
        == 0
    )
    stats = json.loads(capsys.readouterr().out)
    assert stats["db_path"] == str(db.resolve())
    assert stats["indexed"] == 4
    assert cli.main(["embeddings", "build", "--db", str(db), "--rebuild"]) == 0
    assert json.loads(capsys.readouterr().out)["indexed"] == 4
    assert cli.main(["embeddings", "build", "--chunk-size", "100", "--chunk-overlap", "100"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "value_error"
    with pytest.raises(SystemExit):
        cli.main(["embeddings", "build", "--incremental", "--rebuild"])
    with pytest.raises(SystemExit):
        cli.main(["embeddings", "build", "--batch", "0"])


def test_cli_embeddings_status_without_an_index(
    engine_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (engine_env / "data" / "embeddings.db").unlink()
    assert cli.main(["embeddings", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["exists"] is False
    assert cli.main(["embeddings", "search", "fox"]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["type"] == "embedding_index_missing"
    assert error["embedding"]["db_path"]  # the CLI keeps the full, local view
    assert cli.main(["search", "fox", "--mode", "hybrid"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "embedding_index_missing"


def test_cli_embeddings_reports_an_unwritable_database(
    engine_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A directory where the database file should be: SQLite cannot open it.
    assert cli.main(["embeddings", "build", "--db", str(engine_env / "data")]) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["type"] == "io_error"
    assert error["detail"]


def test_package_import_does_not_load_the_legacy_build_module() -> None:
    code = (
        "import sys, knowledge_engine.retrieval.embeddings as e\n"
        "assert 'knowledge_engine.retrieval.embeddings.build' not in sys.modules\n"
        "assert callable(e.build_index) and callable(e.cosine_similarity)\n"
        "assert 'build_index' in dir(e)\n"
        "try:\n"
        "    e.no_such_name\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('AttributeError expected')\n"
    )
    subprocess.run([sys.executable, "-W", "error", "-c", code], check=True)
    # Running the legacy module as a script no longer warns that it was already imported.
    run = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "knowledge_engine.retrieval.embeddings.build",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr


def test_legacy_module_entry_points_delegate_to_the_cli(
    engine_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from knowledge_engine.retrieval.embeddings import build, search

    assert build.main(["--stats"]) == 0
    assert json.loads(capsys.readouterr().out)["built"] is True
    assert build.main(["--incremental"]) == 0
    assert json.loads(capsys.readouterr().out)["indexed"] == 0
    assert search.main(["fox", "--top", "1", "--library", "skills"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
