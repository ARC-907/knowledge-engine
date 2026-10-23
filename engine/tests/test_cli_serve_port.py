"""`knowledge-engine serve` keeps honouring KE_PORT (the deleted serve scripts did)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from knowledge_engine import cli


def _run_serve(monkeypatch: pytest.MonkeyPatch, *argv: str) -> dict[str, Any]:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "serve", *argv])
    assert cli.main() == 0
    assert len(calls) == 1
    return calls[0]


def test_serve_defaults_to_9210(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KE_PORT", raising=False)
    assert _run_serve(monkeypatch)["port"] == 9210


def test_serve_reads_ke_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_PORT", "9999")
    assert _run_serve(monkeypatch)["port"] == 9999


def test_explicit_port_beats_ke_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_PORT", "9999")
    assert _run_serve(monkeypatch, "--port", "9300")["port"] == 9300


def _run_board_serve(monkeypatch: pytest.MonkeyPatch, *argv: str) -> dict[str, Any]:
    from knowledge_engine.agent_board import service

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(service, "serve", lambda **kw: calls.append(kw) or 0)
    monkeypatch.setattr(sys, "argv", ["knowledge-engine", "board-serve", *argv])
    assert cli.main() == 0
    assert len(calls) == 1
    return calls[0]


def test_board_serve_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOARD_HOST", raising=False)
    monkeypatch.delenv("BOARD_PORT", raising=False)
    assert _run_board_serve(monkeypatch) == {"host": "127.0.0.1", "port": 11437}


def test_board_serve_reads_board_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOARD_HOST", "0.0.0.0")  # noqa: S104 -- env value under test, nothing listens
    monkeypatch.setenv("BOARD_PORT", "12000")
    assert _run_board_serve(monkeypatch) == {"host": "0.0.0.0", "port": 12000}  # noqa: S104
    assert _run_board_serve(monkeypatch, "--host", "::1", "--port", "12001") == {
        "host": "::1",
        "port": 12001,
    }


def test_bad_ke_port_is_reported_and_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KE_PORT", "lots")
    assert cli._env_port("KE_PORT", 9210) == 9210
    assert "KE_PORT" in capsys.readouterr().err
