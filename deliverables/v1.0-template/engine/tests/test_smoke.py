"""Smoke tests for the engine."""

from __future__ import annotations

import os
from pathlib import Path


def _env(corpus: Path, data: Path) -> None:
    os.environ["KE_CORPUS_ROOT"] = str(corpus)
    os.environ["KE_DATA_DIR"] = str(data)
    os.environ["KE_REGISTRY_PATH"] = str(corpus / "registry.json")


def test_registry_roundtrip(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    corpus.mkdir()
    data.mkdir()
    _env(corpus, data)

    from knowledge_engine.config import Config
    from knowledge_engine.registry import Registry, RegistryEntry

    config = Config.from_env()
    reg = Registry(config.registry_path, config.data_dir / "registry.db")
    reg.upsert(RegistryEntry(id="lib-x", kind="library", name="X", path="libraries/x"))
    assert reg.get("lib-x") is not None
    reg.set_enabled("lib-x", False)
    assert reg.get("lib-x")["enabled"] is False
    assert reg.remove("lib-x") is True
    assert reg.get("lib-x") is None


def test_kits_are_first_class_registry_entries(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    kitdir = corpus / "kits" / "research-library"
    kitdir.mkdir(parents=True)
    data.mkdir()
    _env(corpus, data)

    from knowledge_engine.config import Config
    from knowledge_engine.registry import Registry
    from knowledge_engine.watcher import auto_register

    config = Config.from_env()
    reg = Registry(config.registry_path, config.data_dir / "registry.db")
    assert auto_register(config, reg) == 1

    kits = reg.list("kit")
    tools = reg.list("tool")
    assert [kit["id"] for kit in kits] == ["kit-research-library"]
    assert kits[0]["path"] == "kits/research-library"
    assert tools == []


def test_indexer_smoke(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    libdir = corpus / "libraries" / "demo"
    libdir.mkdir(parents=True)
    (libdir / "note.md").write_text("the quick brown fox jumps", encoding="utf-8")
    data.mkdir()
    _env(corpus, data)

    from knowledge_engine.config import Config
    from knowledge_engine.registry import Registry, RegistryEntry
    from knowledge_engine.indexer import Indexer

    config = Config.from_env()
    reg = Registry(config.registry_path, config.data_dir / "registry.db")
    reg.upsert(RegistryEntry(id="demo", kind="library", name="Demo", path="libraries/demo"))
    idx = Indexer(config, reg)
    counts = idx.rebuild()
    assert counts["entries"] == 1
    assert counts["files"] == 1
    hits = idx.search("fox")
    assert any("fox" in h["snippet"].lower() or "fox" in h["path"] for h in hits)


def test_app_factory(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    corpus.mkdir()
    data.mkdir()
    _env(corpus, data)
    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        r = client.get("/info")
        assert r.status_code == 200
        assert "kits" in r.json()["counts"]

        r = client.post(
            "/registry",
            json={"id": "kit-x", "kind": "kit", "name": "Kit X", "path": "kits/x"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "kit"

        r = client.get("/registry", params={"kind": "kit"})
        assert r.status_code == 200, r.text
        assert [entry["id"] for entry in r.json()] == ["kit-x"]


def test_search_and_reindex_routes_threadsafe(tmp_path: Path) -> None:
    """Regression: the shared Indexer/Registry on app.state are used from
    FastAPI's worker threadpool. SQLite connections must tolerate cross-thread
    use, or /search and /search/reindex 500 with ProgrammingError.

    This exercises the routes from real worker threads (TestClient runs the
    sync endpoints in a threadpool), which the basic factory test does not.
    """
    import concurrent.futures

    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    libdir = corpus / "libraries" / "demo"
    libdir.mkdir(parents=True)
    (libdir / "note.md").write_text("the quick brown fox", encoding="utf-8")
    data.mkdir()
    _env(corpus, data)

    from fastapi.testclient import TestClient
    from knowledge_engine.app import create_app
    from knowledge_engine.config import Config
    from knowledge_engine.registry import Registry, RegistryEntry

    config = Config.from_env()
    reg = Registry(config.registry_path, config.data_dir / "registry.db")
    reg.upsert(RegistryEntry(id="demo", kind="library", name="Demo", path="libraries/demo"))

    app = create_app()
    with TestClient(app) as client:
        # reindex must succeed (rebuild() uses the shared connection)
        r = client.post("/search/reindex")
        assert r.status_code == 200, r.text
        assert r.json()["files"] == 1

        # search must succeed (search() uses the shared connection)
        r = client.get("/search", params={"q": "fox"})
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1

        # registry toggle must succeed (set_enabled() syncs to SQLite)
        r = client.patch("/registry/demo/toggle", json={"enabled": False})
        assert r.status_code == 200, r.text

        # concurrent searches must not raise
        def _hit() -> int:
            return client.get("/search", params={"q": "fox"}).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda _: _hit(), range(16)))
        assert all(c == 200 for c in codes), codes


def test_capability_inventory_reports_empty_substrates(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    data = tmp_path / "data"
    corpus.mkdir()
    data.mkdir()
    _env(corpus, data)

    from knowledge_engine.cli import _capability_inventory
    from knowledge_engine.config import Config
    from knowledge_engine.registry import Registry

    config = Config.from_env()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    inventory = _capability_inventory(config, registry)

    assert inventory["runtime"] == "knowledge-engine"
    assert inventory["base_mcp"]["tool_count"] == 4
    assert inventory["project_docs"]["tool_count"] >= 40
    assert inventory["board"]["tool_count"] >= 10
    assert inventory["hosted_tools"]["status"] == "available"
    assert inventory["hosted_tools"]["tool_count"] == 0
    assert inventory["sandbox"]["status"] == "available"


def test_base_mcp_registry_schema_includes_kits() -> None:
    from knowledge_engine.mcp_server import TOOLS

    by_name = {tool["name"]: tool for tool in TOOLS}
    assert "kit" in by_name["search"]["inputSchema"]["properties"]["kind"]["enum"]
    assert "kit" in by_name["registry_list"]["inputSchema"]["properties"]["kind"]["enum"]
