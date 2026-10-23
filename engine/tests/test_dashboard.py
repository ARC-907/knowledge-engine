"""Packaged dashboard: shipped as package data and served at /ui."""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def engine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))
    return tmp_path


# ── Dashboard ────────────────────────────────────────────────


def test_dashboard_assets_ship_inside_the_package() -> None:
    static = resources.files("knowledge_engine.server").joinpath("static")
    assert static.joinpath("index.html").is_file()
    assert static.joinpath("vendor", "alpinejs", "3.14.1", "alpine.min.js").is_file()
    assert static.joinpath("vendor", "tailwindcss", "3.4.16", "tailwind.min.js").is_file()


@pytest.mark.usefixtures("engine_env")
def test_ui_serves_dashboard(caplog: pytest.LogCaptureFixture) -> None:
    from knowledge_engine.app import create_app

    with caplog.at_level(logging.INFO, logger="knowledge_engine.server.app"):
        app = create_app()
    assert any("dashboard mounted at /ui" in r.getMessage() for r in caplog.records)

    with TestClient(app) as client:
        r = client.get("/ui/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "<html" in r.text.lower()

        js = client.get("/ui/vendor/alpinejs/3.14.1/alpine.min.js")
        assert js.status_code == 200


@pytest.mark.usefixtures("engine_env")
def test_missing_dashboard_warns_and_is_not_mounted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.server import app as app_module

    monkeypatch.setattr(app_module, "DASHBOARD_DIRNAME", "no-such-static-dir")
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.server.app"):
        app = app_module.create_app()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no-such-static-dir" in r.getMessage() for r in warnings)
    with TestClient(app) as client:
        assert client.get("/ui/").status_code == 404
        assert client.get("/health").status_code == 200
