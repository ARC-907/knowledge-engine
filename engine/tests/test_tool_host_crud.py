"""tool_host: registry CRUD, invocation bookkeeping, static serving and proxying."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib import request as urllib_request

import pytest

from knowledge_engine.runtime.foundation import db
from knowledge_engine.tool_host import host


@pytest.fixture
def tool_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pipeline.db"
    monkeypatch.setenv("KE_PIPELINE_DB", str(path))
    return path


class _Resp:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": 1}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# ── CRUD ─────────────────────────────────────────────────────────────


def test_register_get_list_update_toggle_delete(tool_db: Path) -> None:
    a = host.register_tool("alpha", "static", "/a/", tags=["docs"], local_path="/tmp")
    b = host.register_tool("beta", "service", "b", upstream_url="http://svc.local", tags=["x"])
    assert a["route"] == "a" and a["tags"] == ["docs"] and a["enabled"] == 1
    assert host.get_tool("alpha") == host.get_tool(a["tool_id"])
    assert host.get_tool_by_route("/a") is not None
    assert [t["name"] for t in host.list_tools()] == ["alpha", "beta"]
    assert [t["name"] for t in host.list_tools(kind="service")] == ["beta"]
    assert [t["name"] for t in host.list_tools(tag="docs")] == ["alpha"]

    updated = host.update_tool(a["tool_id"], description="d", tags=["y"], bogus="ignored")
    assert updated is not None and updated["description"] == "d" and updated["tags"] == ["y"]
    assert host.update_tool(a["tool_id"]) == host.get_tool(a["tool_id"])  # nothing to change

    toggled = host.toggle_tool(b["tool_id"])
    assert toggled is not None and toggled["enabled"] == 0
    assert host.get_tool_by_route("b") is None  # disabled tools are not routable
    assert [t["name"] for t in host.list_tools()] == ["alpha"]
    assert [t["name"] for t in host.list_tools(enabled_only=False)] == ["alpha", "beta"]

    assert host.delete_tool(b["tool_id"]) is True
    assert host.delete_tool(b["tool_id"]) is False
    assert host.get_tool("beta") is None
    events = db.get_connection().execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'tool_registered'"
    )
    assert events.fetchone()[0] == 2


def test_register_rejects_unknown_kind_and_duplicates(tool_db: Path) -> None:
    with pytest.raises(ValueError, match="Invalid kind"):
        host.register_tool("x", "lambda", "x")
    host.register_tool("dup", "static", "dup")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        host.register_tool("dup", "static", "other")


def test_record_invocation_counts(tool_db: Path) -> None:
    tool = host.register_tool("c", "static", "c")
    host._record_invocation(tool["tool_id"])
    host._record_invocation(tool["tool_id"])
    again = host.get_tool(tool["tool_id"])
    assert again is not None
    assert again["invocation_count"] == 2 and again["last_invoked_at"]


# ── static ───────────────────────────────────────────────────────────


def test_serve_static_files_directories_and_refusals(tool_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "site"
    (root / "sub").mkdir(parents=True)
    (root / "index.txt").write_text("# hi\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00\x81")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    tool = host.register_tool("site", "static", "site", local_path=str(root))

    page = host.serve_static(tool, "index.txt")
    assert page == {"ok": True, "type": "file", "mime": "text/plain", "content": "# hi\n"}
    listing = host.serve_static(tool)
    assert listing["type"] == "directory"
    assert {e["name"] for e in listing["entries"]} == {"index.txt", "blob.bin", "sub"}
    binary = host.serve_static(tool, "blob.bin")
    assert binary["binary"] is True and binary["size"] == 4
    assert host.serve_static(tool, "../secret.txt")["error"] == "Path traversal not allowed"
    assert host.serve_static(tool, "missing.txt")["ok"] is False
    assert host.serve_static({"tool_id": "t"})["error"] == "No local_path configured"
    gone = host.serve_static({"tool_id": "t", "local_path": str(tmp_path / "nowhere")})
    assert gone["ok"] is False
    counted = host.get_tool(tool["tool_id"])
    assert counted is not None and counted["invocation_count"] == 3


# ── proxy / health ───────────────────────────────────────────────────


def test_proxy_success_records_invocation_and_decodes_json(
    tool_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = host.register_tool("svc", "service", "svc", upstream_url="http://svc.local/api")
    sent: list[urllib_request.Request] = []

    def fake(req: urllib_request.Request, timeout: float) -> _Resp:
        sent.append(req)
        return _Resp(body=b"plain text")

    monkeypatch.setattr(host, "_urlopen", fake)
    out = host.proxy_to_service(tool, method="POST", subpath="items/1", body=b"{}")
    assert out == {"ok": True, "status": 200, "data": "plain text"}
    assert sent[0].full_url == "http://svc.local/api/items/1"
    assert {k.lower(): v for k, v in sent[0].header_items()}["content-type"] == "application/json"
    stored = host.get_tool(tool["tool_id"])
    assert stored is not None and stored["invocation_count"] == 1
    assert host.proxy_to_service({"tool_id": "x"})["error"] == "No upstream_url configured"


def test_proxy_transport_failure_is_reported(
    tool_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = host.register_tool("svc", "service", "svc", upstream_url="http://svc.local")

    def boom(req: Any, timeout: float) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(host, "_urlopen", boom)
    assert host.proxy_to_service(tool) == {"ok": False, "error": "connection refused"}
    assert host.health_check_service(tool) == {"ok": False, "error": "connection refused"}


def test_health_check_all_services(tool_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host.register_tool("up", "service", "up", upstream_url="http://up.local")
    host.register_tool("static", "static", "static")
    monkeypatch.setattr(host, "_urlopen", lambda req, timeout: _Resp(status=204))
    results = host.health_check_all_services()
    assert [(r["name"], r["ok"], r["status"]) for r in results] == [("up", True, 204)]
    assert host.health_check_service({"tool_id": "t"}) == {"ok": False, "error": "No upstream_url"}
