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
    assert static.joinpath("app.js").is_file()
    assert static.joinpath("tailwind.css").is_file()
    assert static.joinpath("vendor", "alpinejs", "3.14.1", "alpine.min.js").is_file()
    # The Tailwind Play-CDN runtime (dev-only per Tailwind) is gone; CSS is compiled.
    assert not static.joinpath("vendor", "tailwindcss").is_dir()


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
def test_ui_links_compiled_css_not_the_tailwind_runtime() -> None:
    from knowledge_engine.app import create_app

    with TestClient(create_app()) as client:
        html = client.get("/ui/").text
        assert 'href="tailwind.css"' in html
        assert "tailwind.min.js" not in html
        assert 'src="app.js"' in html

        css = client.get("/ui/tailwind.css")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")
        assert len(css.content) > 1000

        js = client.get("/ui/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]


def test_dashboard_markup_is_accessible_and_relative() -> None:
    static = resources.files("knowledge_engine.server").joinpath("static")
    html = static.joinpath("index.html").read_text(encoding="utf-8")
    js = static.joinpath("app.js").read_text(encoding="utf-8")

    assert '<html lang="en">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    for landmark in ("<header", "<nav", "<main", 'role="tablist"', 'role="tab"', 'role="tabpanel"'):
        assert landmark in html, landmark
    # Untrusted content (snippets, board messages) is never rendered as HTML.
    assert "x-html=" not in html
    assert "x-html" not in js
    # The dashboard works behind a path prefix: no root-absolute API calls.
    assert "fetch('/" not in js
    assert 'fetch("/' not in js
    assert "new URL('..', location.href)" in js
    # Board requests carry the stored key.
    assert "X-Board-Key" in js
    # Init hooks into Alpine rather than racing it with a timer.
    assert "alpine:init" in js


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
