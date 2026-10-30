"""Browser smoke test of the dashboard.

Opt-in: it needs the ``ui`` dependency group (``uv sync --group ui``) and a
Chromium that Playwright can drive, and runs only with ``KE_UI_TESTS=1``::

    KE_UI_TESTS=1 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \\
        uv run pytest -m ui engine/tests/ui

The engine is served by uvicorn in a background thread from ``create_app()``
over a temporary corpus, so the test is offline and leaves no state behind.
Set ``KE_UI_CHROMIUM=/path/to/chrome`` to force a specific Chromium binary
instead of the one Playwright resolves from ``PLAYWRIGHT_BROWSERS_PATH``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(
        os.environ.get("KE_UI_TESTS") != "1",
        reason="browser smoke tests run only with KE_UI_TESTS=1",
    ),
]


@pytest.fixture
def server_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve: Callable[[Any], Any]
) -> Iterator[str]:
    """The engine serving a one-library corpus whose notes mention 'demo'."""
    corpus = tmp_path / "corpus"
    lib = corpus / "libraries" / "demo"
    lib.mkdir(parents=True)
    (lib / "overview.md").write_text(
        "# Demo library\n\nThis demo note exists so the dashboard search has a demo hit.\n",
        encoding="utf-8",
    )
    (corpus / "registry.json").write_text(
        json.dumps(
            {
                "version": 1,
                "libraries": [
                    {
                        "id": "library-demo",
                        "kind": "library",
                        "name": "Demo Library",
                        "path": "libraries/demo",
                        "enabled": True,
                    }
                ],
                "skills": [],
                "kits": [],
                "tools": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_PIPELINE_DB", str(tmp_path / "data" / "pipeline.db"))

    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.chdir(tmp_path)

    from knowledge_engine.app import create_app
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.retrieval.fts5 import Indexer

    # create_app() does no I/O (app.state is populated by the lifespan), so
    # build the FTS index up front, exactly as `knowledge-engine reindex` does.
    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path)
    indexer = Indexer(config, registry)
    try:
        indexer.rebuild()
    finally:
        indexer.close()
        registry.close()

    with serve(create_app()) as url:
        yield url


def test_dashboard_smoke(server_url: str, page: Any) -> None:
    from playwright.sync_api import expect

    page.goto(f"{server_url}/ui/")
    expect(page).to_have_title("Knowledge Engine — Dashboard")
    expect(page.get_by_role("heading", level=1)).to_have_text("Knowledge Engine")

    # Tabs: roving tabindex, arrow keys move selection and focus.
    search_tab = page.get_by_role("tab", name="search")
    registry_tab = page.get_by_role("tab", name="registry")
    expect(search_tab).to_have_attribute("aria-selected", "true")
    search_tab.focus()
    page.keyboard.press("ArrowRight")
    expect(registry_tab).to_have_attribute("aria-selected", "true")
    expect(registry_tab).to_be_focused()
    expect(page.locator("#panel-registry")).to_be_visible()
    expect(page.locator("#panel-search")).to_be_hidden()
    page.keyboard.press("End")
    expect(page.get_by_role("tab", name="config")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Home")
    expect(search_tab).to_have_attribute("aria-selected", "true")
    expect(page.locator("#panel-search")).to_be_visible()

    # Search: a query for "demo" returns a hit from the temporary corpus.
    page.fill("#search-query", "demo")
    page.keyboard.press("Enter")
    results = page.locator(".search-result")
    expect(results.first).to_be_visible()
    expect(results.first).to_contain_text("demo", ignore_case=True)
    expect(results.first).to_contain_text("library-demo")

    # Theme: follows the (light) OS scheme, toggle flips data-theme and persists.
    html = page.locator("html")
    expect(html).to_have_attribute("data-theme", "light")
    page.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "dark")
    assert page.evaluate("localStorage.getItem('ke-theme')") == "dark"
    page.reload()
    expect(html).to_have_attribute("data-theme", "dark")
    page.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "light")

    assert page.errors == []  # type: ignore[attr-defined]


def test_board_requests_carry_the_stored_key(server_url: str, page: Any) -> None:
    from playwright.sync_api import expect

    page.goto(f"{server_url}/ui/")
    page.get_by_role("tab", name="config").click()
    page.fill("#board-key", "test-board-key")
    page.get_by_role("button", name="Save key").click()
    assert page.evaluate("localStorage.getItem('ke-board-key')") == "test-board-key"

    page.get_by_role("tab", name="board").click()
    with page.expect_request("**/board/messages?*") as req_info:
        page.get_by_role("button", name="Refresh").click()
    assert req_info.value.headers.get("x-board-key") == "test-board-key"

    # Clearing removes the key and the header.
    page.get_by_role("tab", name="config").click()
    page.get_by_role("button", name="Clear").click()
    assert page.evaluate("localStorage.getItem('ke-board-key')") is None
    page.get_by_role("tab", name="board").click()
    with page.expect_request("**/board/messages?*") as req_info:
        page.get_by_role("button", name="Refresh").click()
    assert "x-board-key" not in req_info.value.headers
    expect(page.locator("#panel-board")).to_be_visible()
    assert page.errors == []  # type: ignore[attr-defined]
