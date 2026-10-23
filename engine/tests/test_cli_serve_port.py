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


def test_bad_ke_port_is_reported_and_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KE_PORT", "lots")
    assert cli._env_port("KE_PORT", 9210) == 9210
    assert "KE_PORT" in capsys.readouterr().err
