"""`knowledge-engine board` client: httpx transport, error mapping, settings, scopes create."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from knowledge_engine.agent_board import cli as board_cli
from knowledge_engine.core.settings import Settings


def _fake_request(calls: list[dict[str, Any]], response: httpx.Response | Exception) -> Any:
    def fake(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls.append({"method": method, "url": url, **kwargs})
        if isinstance(response, Exception):
            raise response
        return response

    return fake


def test_base_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert board_cli._base_url("http://x:1/", Settings()) == "http://x:1"
    assert board_cli._base_url(None, Settings(board_url="http://b:2/")) == "http://b:2"
    assert board_cli._base_url(None, Settings(board_port="7000", port=8000)) == (
        "http://127.0.0.1:7000"
    )
    assert board_cli._base_url(None, Settings(port=8000)) == "http://127.0.0.1:8000"
    for name in ("KE_BOARD_URL", "KE_BOARD_PORT", "KE_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert board_cli._base_url(None) == "http://127.0.0.1:9210"


def test_status_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        httpx, "request", _fake_request(calls, httpx.Response(200, json={"ok": True}))
    )
    monkeypatch.setenv("KE_BOARD_KEY", "secret-key")
    assert board_cli.main(["--url", "http://board:1", "status"]) == 0
    assert '"ok": true' in capsys.readouterr().out
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://board:1/board/status"
    assert calls[0]["timeout"] == board_cli.REQUEST_TIMEOUT


def test_unreachable_board_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(httpx, "request", _fake_request([], httpx.ConnectError("refused")))
    assert board_cli.main(["--url", "http://board:1", "channels"]) == 2
    assert "board unreachable at http://board:1" in capsys.readouterr().err


def test_http_error_returns_1_with_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        httpx, "request", _fake_request([], httpx.Response(403, text="admin key required"))
    )
    assert board_cli.main(["--url", "http://board:1", "sweep"]) == 1
    assert "sweep rejected (403): admin key required" in capsys.readouterr().err


def test_post_sends_key_and_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        httpx, "request", _fake_request(calls, httpx.Response(201, json={"message_id": "m1"}))
    )
    rc = board_cli.main(
        [
            "--url",
            "http://board:1",
            "--key",
            "k1",
            "post",
            "--from",
            "agent",
            "--type",
            "status",
            "--body",
            "hello",
            "--scope",
            "proj-a",
        ]
    )
    assert rc == 0
    call = calls[0]
    assert call["method"] == "POST"
    assert call["headers"] == {"X-Board-Key": "k1"}
    assert call["json"]["body"] == "hello"
    assert call["json"]["scope"] == "proj-a"


def test_scopes_create_puts_with_admin_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        httpx,
        "request",
        _fake_request(calls, httpx.Response(201, json={"scope": "feat-x", "created": True})),
    )
    assert (
        board_cli.main(["--url", "http://b:1", "--key", "adm", "scopes", "create", "feat-x"]) == 0
    )
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"] == "http://b:1/board/scopes/feat-x"
    assert calls[0]["headers"] == {"X-Board-Key": "adm"}
    assert '"created": true' in capsys.readouterr().out

    calls.clear()
    monkeypatch.setattr(
        httpx, "request", _fake_request(calls, httpx.Response(200, json={"scopes": []}))
    )
    assert board_cli.main(["--url", "http://b:1", "scopes"]) == 0
    assert board_cli.main(["--url", "http://b:1", "scopes", "list"]) == 0
    assert [c["method"] for c in calls] == ["GET", "GET"]


def test_empty_body_decodes_to_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "request", _fake_request([], httpx.Response(204)))
    assert board_cli._delete("http://b:1/board/keys/x") == {"ok": True}
