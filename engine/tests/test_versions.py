"""Every surface reports the installed distribution's version."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

DIST_VERSION = importlib.metadata.version("knowledge-engine")


@pytest.fixture
def engine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))
    return tmp_path


# ── Version reporting ────────────────────────────────────────


def test_package_version_matches_distribution_metadata() -> None:
    import knowledge_engine

    assert knowledge_engine.__version__ == DIST_VERSION


@pytest.mark.usefixtures("engine_env")
def test_health_info_and_openapi_report_distribution_version() -> None:
    from knowledge_engine.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/health").json()["version"] == DIST_VERSION
        assert client.get("/info").json()["version"] == DIST_VERSION
        assert client.get("/openapi.json").json()["info"]["version"] == DIST_VERSION


@pytest.mark.usefixtures("engine_env")
def test_mcp_server_info_reports_distribution_version() -> None:
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.mcp.server import build_server
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    server = build_server(config, registry, Indexer(config, registry), include_groups=False)

    async def server_info() -> Any:
        async with Client(server) as client:
            return client.server_info

    info = anyio.run(server_info)

    assert info is not None
    assert (info.name, info.version) == ("knowledge-engine", DIST_VERSION)


@pytest.mark.usefixtures("engine_env")
def test_standalone_board_health_reports_distribution_version() -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    with TestClient(create_standalone_app()) as client:
        body = client.get("/health").json()
    assert body["version"] == DIST_VERSION
    # Same health shape as the engine's /health, plus the historical "ok" flag.
    assert body["status"] == "ok"
    assert body["ok"] is True
