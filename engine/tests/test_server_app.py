"""Engine app: I/O-free factory, lifespan-owned resources, DI, models, error handlers."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_engine.core.settings import Settings
from knowledge_engine.generation import ProviderError

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


def _app(**settings: Any) -> FastAPI:
    from knowledge_engine.app import create_app

    return create_app(Settings(**settings)) if settings else create_app()


# ── Construction is free of I/O ──────────────────────────────────


def test_create_app_makes_no_network_or_filesystem_calls(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("create_app() must not perform I/O")

    for name in ("get", "post", "request", "send"):
        monkeypatch.setattr(httpx.Client, name, forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setenv("KE_CLOUD_ENDPOINT", "https://llm.example/v1")
    monkeypatch.setenv("KE_CLOUD_API_KEY", "k")
    monkeypatch.setenv("KE_MCP_HTTP", "1")

    app = _app()

    assert not (engine_env / "data").exists()
    for name in ("registry", "indexer", "providers", "config", "sweeper"):
        assert not hasattr(app.state, name), name
    assert isinstance(app.state.settings, Settings)


def test_routes_outside_the_lifespan_answer_503(engine_env: Path) -> None:
    client = TestClient(_app())  # no context manager: the lifespan never runs
    r = client.get("/info")
    assert r.status_code == 503
    assert "engine not started" in r.json()["detail"]
    assert client.get("/health").status_code == 200


def test_explicit_settings_drive_the_app(engine_env: Path) -> None:
    with TestClient(_app(board_enabled=False), client=LOOPBACK) as client:
        assert client.get("/board/status").status_code == 404
        assert client.get("/health").json()["status"] == "ok"
    with TestClient(_app(), client=LOOPBACK) as client:
        assert client.get("/board/status").status_code == 200


# ── Lifespan ownership ───────────────────────────────────────────


def test_lifespan_opens_and_closes_the_databases(engine_env: Path) -> None:
    app = _app()
    with TestClient(app) as client:
        registry = app.state.registry
        indexer = app.state.indexer
        assert (engine_env / "data" / "registry.db").is_file()
        assert client.get("/search", params={"q": "x"}).status_code == 200
        assert not registry.closed and not indexer.closed
    assert registry.closed and indexer.closed
    with pytest.raises(sqlite3.ProgrammingError):
        registry.set_enabled("anything", True)
    with pytest.raises(sqlite3.ProgrammingError):
        indexer.search("x")
    # A new lifespan reopens fresh connections.
    with TestClient(app) as client:
        assert app.state.registry is not registry
        assert client.get("/registry").status_code == 200


def test_lifespan_sweeper_is_owned_and_default_restored(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.agent_board import sweeper

    before = sweeper.default_sweeper()
    monkeypatch.setenv("KE_BOARD_SWEEPER", "1")
    app = _app()
    with TestClient(app, client=LOOPBACK) as client:
        owned = app.state.sweeper
        assert sweeper.default_sweeper() is owned
        assert owned.is_running()
        assert client.get("/board/status").json()["sweeper_running"] is True
    assert not owned.is_running()
    assert sweeper.default_sweeper() is before


def test_startup_probe_warns_about_unreachable_providers(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.generation.ollama_provider import OllamaProvider
    from knowledge_engine.server import lifespan as lifespan_mod

    monkeypatch.setattr(OllamaProvider, "available", lambda _self: False)
    threads: list[threading.Thread] = []
    real = lifespan_mod._start_provider_probe
    monkeypatch.setattr(
        lifespan_mod, "_start_provider_probe", lambda p: threads.append(real(p)) or threads[-1]
    )
    # An explicitly configured endpoint that does not answer is a WARNING ...
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
    with (
        caplog.at_level(logging.INFO, logger="knowledge_engine.generation.base"),
        TestClient(_app(provider_startup_probe=True)) as client,
    ):
        threads[0].join(timeout=10)
        # The cached probe result is reused: /generate falls through to echo.
        r = client.post("/generate", json={"prompt": "hi"})
        assert r.json()["provider"] == "echo"
    warned = [r for r in caplog.records if "'ollama'" in r.getMessage()]
    assert warned and all(r.levelno == logging.WARNING for r in warned)

    # ... while the built-in default endpoint is only reported at INFO.
    caplog.clear()
    monkeypatch.delenv("OLLAMA_BASE_URL")
    with (
        caplog.at_level(logging.INFO, logger="knowledge_engine.generation.base"),
        TestClient(_app(provider_startup_probe=True)) as client,
    ):
        threads[-1].join(timeout=10)
        assert client.post("/generate", json={"prompt": "hi"}).json()["provider"] == "echo"
    quiet = [r for r in caplog.records if "'ollama'" in r.getMessage()]
    assert quiet and all(r.levelno == logging.INFO for r in quiet)


# ── Validation ───────────────────────────────────────────────────


def test_invalid_registry_kind_is_422(engine_env: Path) -> None:
    with TestClient(_app()) as client:
        assert client.get("/registry", params={"kind": "bogus"}).status_code == 422
        assert client.get("/registry", params={"kind": "kit"}).status_code == 200
        r = client.post("/registry", json={"id": "x", "kind": "bogus", "name": "X", "path": "x"})
        assert r.status_code == 422


@pytest.mark.parametrize(("limit", "status"), [(1000, 422), (0, 422), (101, 422), (100, 200)])
def test_search_limit_is_bounded(engine_env: Path, limit: int, status: int) -> None:
    with TestClient(_app()) as client:
        r = client.get("/search", params={"q": "fox", "limit": limit})
        assert r.status_code == status, r.text


def test_entry_tags_default_is_not_shared() -> None:
    from knowledge_engine.server.schemas import EntryIn

    a = EntryIn(id="a", kind="kit", name="A", path="a")
    b = EntryIn(id="b", kind="kit", name="B", path="b")
    a.tags.append("x")
    assert b.tags == []


def test_generate_input_is_validated(engine_env: Path) -> None:
    with TestClient(_app()) as client:
        assert client.post("/generate", json={"prompt": "x", "max_tokens": 0}).status_code == 422
        assert client.post("/generate", json={"prompt": "x", "temperature": -1}).status_code == 422
        r = client.post("/generate", json={"prompt": "x", "provider": "echo"})
        assert r.status_code == 200
        assert r.json() == {"text": "x", "provider": "echo", "model": "echo-1", "usage": None}


# ── Concurrency ──────────────────────────────────────────────────


def test_concurrent_registry_toggles_from_eight_threads(engine_env: Path) -> None:
    ids = [f"kit-{i}" for i in range(8)]
    with TestClient(_app()) as client:
        for entry_id in ids:
            r = client.post(
                "/registry", json={"id": entry_id, "kind": "kit", "name": entry_id, "path": "k"}
            )
            assert r.status_code == 200

        def hammer(n: int) -> list[int]:
            codes = []
            for round_ in range(25):
                target = ids[(n + round_) % len(ids)]
                r = client.patch(f"/registry/{target}/toggle", json={"enabled": round_ % 2 == 0})
                codes.append(r.status_code)
            return codes

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(hammer, range(8)))
        assert all(code == 200 for codes in results for code in codes)

        listed = client.get("/registry").json()
        assert sorted(e["id"] for e in listed) == ids
    # The JSON source of truth is intact and agrees with the mirror.
    doc = json.loads((engine_env / "corpus" / "registry.json").read_text(encoding="utf-8"))
    assert sorted(row["id"] for row in doc["kits"]) == ids
    with sqlite3.connect(engine_env / "data" / "registry.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 8
    leftovers = list((engine_env / "corpus").glob(".registry.json.*.tmp"))
    assert leftovers == []


# ── Response models describe the existing payloads ───────────────


def test_hand_edited_registry_rows_are_served_unchanged(engine_env: Path) -> None:
    row = {"id": "lib-a", "name": "A", "path": "libraries/a", "custom": {"x": 1}, "tags": "a,b"}
    doc = {
        "version": 1,
        "libraries": [row],
        "skills": [],
        "kits": [],
        "tools": [],
        "lifecycle": {"watch_enabled": True, "auto_register": False, "note": "kept"},
    }
    (engine_env / "corpus" / "registry.json").write_text(json.dumps(doc), encoding="utf-8")
    with TestClient(_app()) as client:
        assert client.get("/registry").json() == [{**row, "kind": "library"}]
        assert client.get("/registry/lib-a").json() == {**row, "kind": "library"}
        assert client.get("/registry/lifecycle/state").json() == doc["lifecycle"]
        info = client.get("/info").json()
        assert info["lifecycle"] == doc["lifecycle"]
        assert info["counts"] == {"libraries": 1, "skills": 0, "kits": 0, "tools": 0}
        assert info["providers"] == ["ollama", "echo"]
        removed = client.delete("/registry/lib-a").json()
        assert removed == {"removed": "lib-a"}


# ── Exception handlers ───────────────────────────────────────────


def test_global_exception_handlers(engine_env: Path, caplog: pytest.LogCaptureFixture) -> None:
    app = _app()

    @app.get("/_raise/value")
    def raise_value() -> None:
        raise ValueError("bad input here")

    @app.get("/_raise/db")
    def raise_db() -> None:
        raise sqlite3.OperationalError("database is locked")

    @app.get("/_raise/provider")
    def raise_provider() -> None:
        raise ProviderError("cloud-http", "upstream returned HTTP 500", status_code=500)

    with (
        caplog.at_level(logging.WARNING),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        r = client.get("/_raise/value")
        assert (r.status_code, r.json()) == (
            400,
            {"detail": "bad input here", "type": "value_error"},
        )
        r = client.get("/_raise/db")
        assert r.status_code == 503
        assert r.json() == {
            "detail": "database unavailable; retry later",
            "type": "database_unavailable",
        }
        r = client.get("/_raise/provider")
        assert r.status_code == 502
        assert r.json() == {
            "detail": "cloud-http: upstream returned HTTP 500",
            "type": "provider_error",
        }
    db_errors = [r for r in caplog.records if "database error" in r.getMessage()]
    assert db_errors and db_errors[0].exc_info is not None


def test_generate_with_cloud_provider_uses_the_mocked_endpoint(
    engine_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.generation import cloud_http

    replies = {"status": 200}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        if replies["status"] != 200:
            return httpx.Response(replies["status"], text="nope")
        return httpx.Response(
            200,
            json={
                "model": "gpt-x",
                "choices": [{"message": {"role": "assistant", "content": "from the cloud"}}],
                "usage": {"total_tokens": 3},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cloud_http.CloudHTTPProvider,
        "_client",
        lambda self, timeout: httpx.Client(transport=transport, timeout=timeout),
    )
    settings = {
        "cloud_endpoint": "https://llm.example/v1",
        "cloud_api_key": "sk-app",
        "cloud_model": "gpt-x",
    }
    with TestClient(_app(**settings)) as client:
        assert client.get("/generate/providers").json() == ["cloud-http", "ollama", "echo"]
        r = client.post("/generate", json={"prompt": "hello"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "text": "from the cloud",
            "provider": "cloud-http",
            "model": "gpt-x",
            "usage": {"total_tokens": 3},
        }
        replies["status"] = 500
        r = client.post("/generate", json={"prompt": "hello"})
        assert r.status_code == 502
        assert r.json()["type"] == "provider_error"
    assert seen[-1].headers["authorization"] == "Bearer sk-app"


# ── Board request models keep the historical 400 answers ─────────


def test_board_post_validation_stays_400(engine_env: Path) -> None:
    with TestClient(_app(), client=LOOPBACK) as client:
        r = client.post("/board/messages", json={"channel": "ops"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "message_type is required" in detail
        assert "sender_node_id is required" in detail
        r = client.post(
            "/board/messages",
            json={"channel": "nope", "message_type": "x", "sender_node_id": "n", "body": "b"},
        )
        assert r.status_code == 400
        assert "channel 'nope'" in r.json()["detail"]
        r = client.post(
            "/board/messages",
            json={"message_type": "x", "sender_node_id": "n", "body": "b", "ttl_hours": "24"},
        )
        assert r.status_code == 201, r.text
        assert client.post("/board/messages", json=[1, 2]).status_code == 422
        r = client.post(f"/board/messages/{r.json()['message_id']}/ack", json={})
        assert r.status_code == 400


def test_board_bodies_are_documented_from_the_field_tables(engine_env: Path) -> None:
    from knowledge_engine.agent_board import http as board_http, schemas, store

    spec = _app().openapi()
    message = spec["components"]["schemas"]["MessageIn"]
    for field in schemas.MESSAGE_FIELDS:
        assert field.name in message["properties"]
        if field.max_len is not None:
            assert message["properties"][field.name]["maxLength"] == field.max_len
    assert set(message["required"]) == {"message_type", "sender_node_id", "body"}
    assert set(board_http.CONFIG_FIELDS) == set(store._CONFIG_INT_FIELDS) | {"channels"}
    post = spec["paths"]["/board/messages"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/MessageIn"
    )
