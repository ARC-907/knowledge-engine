"""Board HTTP layering, the Sweeper object, the standalone service and registry durability."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from knowledge_engine.agent_board import sweeper
from knowledge_engine.core.settings import Settings

SRC = Path(__file__).resolve().parents[1] / "src" / "knowledge_engine"
LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def board_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))
    return tmp_path


# ── Layering ─────────────────────────────────────────────────────


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_agent_board_never_imports_the_server_package() -> None:
    for path in (SRC / "agent_board").rglob("*.py"):
        offenders = {m for m in _imports(path) if "server" in m.split(".")}
        assert not offenders, f"{path.name} imports {offenders}"


def test_board_routes_is_an_alias_of_agent_board_http() -> None:
    from knowledge_engine.agent_board import http as board_http
    from knowledge_engine.server.api import board_routes

    assert board_routes is board_http
    assert board_routes.router is board_http.router


def test_trust_helpers_follow_explicit_settings() -> None:
    from knowledge_engine.agent_board import http as board_http

    s = Settings(board_peer_allowlist="10.0.0.0/8", trust_proxy=True, proxy_allowlist="")
    assert [str(n) for n in board_http._allowed_networks(s)] == ["10.0.0.0/8"]
    assert board_http._trust_proxy_enabled(s) is True
    assert board_http._allowed_proxy_networks(s) == []
    policy = board_http.trust_policy(s)
    assert policy["trusted_cidrs"] == ["10.0.0.0/8"]
    assert policy["trust_proxy"] is True
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.1.2.3"), headers={}, app=SimpleNamespace(state=None)
    )
    assert board_http._is_trusted_peer(request, s) is True  # type: ignore[arg-type]
    assert board_http._is_trusted_peer(request, Settings()) is False  # type: ignore[arg-type]


# ── Sweeper object ───────────────────────────────────────────────


class _FakeStore:
    """Minimal stand-in for the store facade: records what the pass touched."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def load_config(self) -> dict[str, Any]:
        self.calls.append("load_config")
        return {
            "max_messages_before_prune": 10,
            "stale_blocker_hours": 2,
            "digest_interval_minutes": 0,
            "channels": [],
            "sweep_interval_s": 60,
            "sweeper_enabled": 1,
        }

    def prune_expired(self) -> int:
        self.calls.append("prune_expired")
        return 2

    def prune_by_count(self, _max: int) -> int:
        return 1

    def get_unacked_blockers(self, threshold_hours: int) -> list[Any]:
        return []

    def record_sweep(self, **_kw: Any) -> None:
        self.calls.append("record_sweep")


@pytest.mark.usefixtures("board_env")
def test_sweeper_runs_against_its_injected_store(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStore()
    monkeypatch.setattr(sweeper.scopes, "list_scopes", list)
    swp = sweeper.Sweeper(store_module=fake, settings=Settings(board_sweeper=False))  # type: ignore[arg-type]
    result = swp.sweep_once(force=True)
    assert result["pruned_expired"] == 2
    assert result["pruned_overflow"] == 1
    assert "record_sweep" in fake.calls
    assert swp.env_override() is False
    assert swp.should_run() is False


@pytest.mark.usefixtures("board_env")
def test_sweeper_instances_are_independent_and_default_is_swappable() -> None:
    a = sweeper.Sweeper(settings=Settings())
    b = sweeper.Sweeper(settings=Settings())
    previous = sweeper.install_default(a)
    try:
        assert a.start() is True
        assert a.start() is False
        assert sweeper.is_running() is True
        assert b.is_running() is False
        assert sweeper.stop() is True
        assert not a.is_running()
    finally:
        sweeper.install_default(previous)
    assert sweeper.default_sweeper() is not a


@pytest.mark.usefixtures("board_env")
def test_should_run_follows_the_persisted_flag_when_env_is_unset() -> None:
    from knowledge_engine.agent_board import store

    swp = sweeper.Sweeper(settings=Settings(board_sweeper=None))
    assert swp.should_run() is True
    store.update_config({"sweeper_enabled": 0})
    assert swp.should_run() is False
    assert sweeper.Sweeper(settings=Settings(board_sweeper=True)).should_run() is True


# ── Standalone service ───────────────────────────────────────────


@pytest.mark.usefixtures("board_env")
def test_standalone_app_owns_its_sweeper_and_uses_given_settings() -> None:
    from knowledge_engine.agent_board.service import create_standalone_app

    before = sweeper.default_sweeper()
    app = create_standalone_app(
        Settings(board_sweeper=True, board_cors_origins="https://ops.example")
    )
    assert not hasattr(app.state, "sweeper")
    with TestClient(app, client=LOOPBACK) as client:
        owned = app.state.sweeper
        assert owned.is_running() and sweeper.default_sweeper() is owned
        assert client.get("/board/status").json()["sweeper_running"] is True
        r = client.options(
            "/board/status",
            headers={
                "Origin": "https://ops.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers["access-control-allow-origin"] == "https://ops.example"
        r = client.post("/board/messages", json={"channel": "ops"})
        assert r.status_code == 400
        assert r.json()["type"] == "payload_invalid"
    assert not owned.is_running()
    assert sweeper.default_sweeper() is before


def test_board_serve_port_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine import cli
    from knowledge_engine.agent_board import service

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(service, "serve", lambda **kw: calls.append(kw) or 0)
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "board-serve"])
    monkeypatch.setenv("KE_BOARD_STANDALONE_PORT", "12345")
    assert cli.main() == 0
    monkeypatch.setenv("BOARD_PORT", "12346")
    assert cli.main() == 0
    assert [c["port"] for c in calls] == [12345, 12346]


# ── Registry durability ──────────────────────────────────────────


def test_registry_write_failure_leaves_the_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge_engine.core import registry as registry_mod
    from knowledge_engine.core.registry import Registry, RegistryEntry

    reg = Registry(tmp_path / "registry.json", tmp_path / "registry.db")
    reg.upsert(RegistryEntry(id="a", kind="kit", name="A", path="a"))
    before = (tmp_path / "registry.json").read_text(encoding="utf-8")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(registry_mod.json, "dumps", boom)
    with pytest.raises(OSError, match="disk full"):
        reg.set_enabled("a", False)
    assert (tmp_path / "registry.json").read_text(encoding="utf-8") == before
    assert json.loads(before)["kits"][0]["id"] == "a"
    assert list(tmp_path.glob(".registry.json.*")) == []
    reg.close()
    reg.close()  # idempotent
    assert reg.closed


# ── MCP mount attaches its server late ───────────────────────────


def test_mcp_mount_requires_an_attached_server() -> None:
    import anyio

    from knowledge_engine.mcp.http import StreamableHTTPMount

    mount = StreamableHTTPMount(host="127.0.0.1")

    async def enter() -> None:
        async with mount.lifespan():
            pass

    with pytest.raises(RuntimeError, match="attach"):
        anyio.run(enter)
