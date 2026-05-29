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
