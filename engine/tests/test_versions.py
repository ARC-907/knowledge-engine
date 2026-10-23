"""Every surface reports the installed distribution's version."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    from knowledge_engine.mcp.server import Server

    resp = Server().handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    assert resp is not None
    assert resp["result"]["serverInfo"] == {"name": "knowledge-engine", "version": DIST_VERSION}


@pytest.mark.usefixtures("engine_env")
def test_standalone_board_health_reports_distribution_version() -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    with TestClient(create_standalone_app()) as client:
        assert client.get("/health").json()["version"] == DIST_VERSION
