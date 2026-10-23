"""Connection lifecycle: every entry point closes what it opened.

* the app lifespan closes the pipeline/board DB connections (from every
  worker thread) and clears the engine objects, so late requests get 503;
* the CLI closes them before returning;
* ``/generate`` bodies are capped like ``/board`` bodies.
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from knowledge_engine.runtime.foundation import db

LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def engine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    return tmp_path


def test_lifespan_closes_board_connections_from_every_thread(engine_env: Path) -> None:
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app, client=LOOPBACK) as client:
        for i in range(5):
            r = client.post(
                "/board/messages",
                json={"message_type": "chatter", "sender_node_id": "t", "body": f"b{i}"},
            )
            assert r.status_code == 201, r.text
        assert client.get("/board/status").status_code == 200
        assert db.open_connection_count() >= 1
    assert db.open_connection_count() == 0


def test_routes_answer_503_after_shutdown(engine_env: Path) -> None:
    from knowledge_engine.app import create_app

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/search", params={"q": "x"}).status_code == 200
    for name in ("registry", "indexer", "providers", "config"):
        assert getattr(app.state, name) is None, name
    late = TestClient(app)  # no lifespan: the app is shut down
    r = late.get("/search", params={"q": "x"})
    assert r.status_code == 503
    assert "engine not started" in r.text
    assert late.get("/registry").status_code == 503


def test_generate_body_is_capped(engine_env: Path) -> None:
    from knowledge_engine.app import create_app
    from knowledge_engine.server.api import generate_routes

    with TestClient(create_app()) as client:
        too_big = "x" * (generate_routes.MAX_REQUEST_BODY_BYTES + 1)
        r = client.post("/generate", json={"prompt": too_big})
        assert r.status_code == 413
        # Small bodies still reach the route (no provider configured -> 503/502).
        assert client.post("/generate", json={"prompt": "hi"}).status_code != 413


def test_cli_closes_pipeline_connections(engine_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine import cli

    def handler(_args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
        db.get_connection().execute("SELECT 1")
        assert db.open_connection_count() == 1
        return 0

    monkeypatch.setattr(cli, "_load_handler", lambda module, func: handler)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        assert cli.main(["board", "status"]) == 0
    assert db.open_connection_count() == 0


def test_standalone_board_service_closes_connections(engine_env: Path) -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    with TestClient(create_standalone_app(), client=LOOPBACK) as client:
        assert client.get("/board/status").status_code == 200
        assert db.open_connection_count() >= 1
    assert db.open_connection_count() == 0


def test_mcp_run_closes_everything(engine_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.mcp import server as mcp_server

    closed: list[str] = []
    real_shutdown = mcp_server._shutdown

    def spy(registry: Any, indexer: Any) -> None:
        real_shutdown(registry, indexer)
        closed.append("done")
        assert registry.closed and indexer.closed

    def fake_serve(_args: argparse.Namespace, _server: Any) -> int:
        db.get_connection()  # a board tool call would open this
        return 0

    monkeypatch.setattr(mcp_server, "configure_logging", lambda: None)
    monkeypatch.setattr(mcp_server, "_shutdown", spy)
    monkeypatch.setattr(mcp_server, "_serve", fake_serve)
    args = argparse.Namespace(selftest=False, http=False, host="127.0.0.1", port=0)
    assert mcp_server.run(args) == 0
    assert closed == ["done"]
    assert db.open_connection_count() == 0
