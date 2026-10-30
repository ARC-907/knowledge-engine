"""Browser tests of the dashboard's Knowledge and Modules views (V2 P8).

Same opt-in as ``test_dashboard_playwright.py`` (``KE_UI_TESTS=1`` and a
Chromium Playwright can drive). The server runs over a temporary corpus that
has been migrated into ``knowledge.db``, classified into two projects and a
collection, and recorded in the evidence store, plus one registered module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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

PROBE = Path(__file__).resolve().parents[1] / "fixtures" / "modules" / "probe"
WS = "default"
DOCS = {
    "libs/field/tides.md": "# Tides\n\nField notes on spring tides at the harbour.\n",
    "libs/field/owls.md": "# Owls\n\nField notes on owls in the forest.\n",
    "libs/field/weather.md": "# Weather\n\nField notes on the weather for everyone.\n",
}


@dataclass
class Site:
    url: str
    app: Any
    corpus: Path
    admin_key: str


@pytest.fixture
def site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve: Callable[[Any], Any]
) -> Iterator[Site]:
    corpus = tmp_path / "corpus"
    for rel, text in DOCS.items():
        (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
        (corpus / rel).write_bytes(text.encode("utf-8"))
    (corpus / "registry.json").write_text(
        json.dumps(
            {
                "version": 1,
                "libraries": [
                    {
                        "id": "library-field",
                        "kind": "library",
                        "name": "Field notes",
                        "path": "libs/field",
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
    modules_dir = tmp_path / "operator-modules"
    shutil.copytree(PROBE, modules_dir / "probe")
    data = tmp_path / "data"
    for key, value in {
        "KE_CORPUS_ROOT": corpus,
        "KE_DATA_DIR": data,
        "KE_PIPELINE_DB": data / "pipeline.db",
        "KE_REGISTRY_PATH": corpus / "registry.json",
        "KE_MODULES_DIR": modules_dir,
        "KE_CONFIG_PATH": tmp_path / "absent.toml",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.chdir(tmp_path)

    from knowledge_engine.agent_board import keys
    from knowledge_engine.app import create_app
    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.knowledge import backfill
    from knowledge_engine.knowledge.backfill import corpus_record_id
    from knowledge_engine.knowledge.evidence.snapshot import snapshot
    from knowledge_engine.knowledge.evidence.store import EvidenceStore
    from knowledge_engine.knowledge.store import KnowledgeStore
    from knowledge_engine.modules.config import ModulesConfig
    from knowledge_engine.modules.dispatcher import ModuleHost
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path)
    indexer = Indexer(config, registry)
    try:
        indexer.rebuild()
    finally:
        indexer.close()
        registry.close()
    backfill.apply(
        corpus_root=corpus,
        registry_path=corpus / "registry.json",
        db_path=data / "knowledge.db",
        workspace_id=WS,
    )
    with KnowledgeStore.open(data / "knowledge.db") as store:
        store.create_project(WS, "Harbour", project_id="harbour")
        store.create_project(WS, "Forest", project_id="forest")
        tides = corpus_record_id("libs/field/tides.md")
        owls = corpus_record_id("libs/field/owls.md")
        store.classify("corpus:base", tides, project_id="harbour", research_type="note")
        store.classify("corpus:base", owls, project_id="forest", research_type="article")
        store.create_collection(WS, "Reading list", collection_id="reading", project_id="harbour")
        store.add_members("reading", [("corpus:base", tides)])
        snapshot(
            EvidenceStore(store),
            corpus_root=corpus,
            registry_path=corpus / "registry.json",
            apply=True,
        )

    host = ModuleHost.open(ModulesConfig.from_env())
    try:
        host.register(modules_dir / "probe")
        host.enable("test.probe")
    finally:
        host.close(drain_s=1.0)
    master = keys.ensure_master_key()
    assert master is not None

    app = create_app()
    with serve(app) as url:
        yield Site(url, app, corpus, master["raw_key"])


def _open_tab(page: Any, site: Site, name: str) -> None:
    page.goto(f"{site.url}/ui/")
    page.get_by_role("tab", name=name).click()


def _search(page: Any, query: str) -> None:
    page.fill("#kn-query", query)
    page.locator("#kn-query").press("Enter")


def test_knowledge_filters_change_results(site: Site, page: Any) -> None:
    from playwright.sync_api import expect

    _open_tab(page, site, "knowledge")
    expect(page.locator("#kn-workspace")).to_have_value(WS)
    _search(page, "field notes")
    hits = page.locator(".knowledge-hit")
    expect(hits).to_have_count(3)
    expect(page.locator(".source-status").first).to_have_attribute("data-state", "ok")
    expect(page.locator("#kn-partial")).to_be_hidden()
    # Excerpt markup from the index is rendered as <mark> elements, never as raw HTML.
    expect(hits.first.locator("mark").first).to_be_visible()

    # Project filter: only the harbour record.
    page.select_option("#kn-project", "harbour")
    _search(page, "field notes")
    expect(hits).to_have_count(1)
    expect(hits.first).to_contain_text("Tides")
    expect(hits.first.locator(".hit-project")).to_have_text("Harbour")
    expect(hits.first.locator(".hit-collections")).to_have_text("Reading list")

    # ... plus records outside any project.
    page.check("#kn-include-workspace")
    _search(page, "field notes")
    expect(hits).to_have_count(2)

    # Collection filter (whole workspace again): only the collection's member.
    page.select_option("#kn-project", "")
    page.locator("details summary", has_text="Filters").click()
    page.get_by_label("Reading list").check()
    _search(page, "field notes")
    expect(hits).to_have_count(1)
    expect(hits.first).to_contain_text("Tides")

    # Research-type filter replaces it: the article.
    page.get_by_label("Reading list").uncheck()
    page.get_by_label("article").check()
    _search(page, "field notes")
    expect(hits).to_have_count(1)
    expect(hits.first).to_contain_text("Owls")
    assert page.errors == []  # type: ignore[attr-defined]


def test_scope_errors_are_reported_not_hidden(site: Site, page: Any) -> None:
    from playwright.sync_api import expect

    _open_tab(page, site, "knowledge")
    page.locator("details summary", has_text="Filters").click()
    page.fill("#kn-tags", "no-such-tag")
    _search(page, "field notes")
    expect(page.locator(".knowledge-hit")).to_have_count(0)
    expect(page.get_by_text("No hits in this scope.")).to_be_visible()
    # Vector mode without an embedding index: every source fails -> a typed error.
    page.fill("#kn-tags", "")
    page.select_option("#kn-mode", "vector")
    _search(page, "field notes")
    expect(page.locator("#kn-error")).to_contain_text("unavailable")
    assert page.errors == []  # type: ignore[attr-defined]


def test_open_shows_the_exact_revision_and_citation_states(site: Site, page: Any) -> None:
    from playwright.sync_api import expect

    _open_tab(page, site, "knowledge")
    _search(page, "spring tides")
    hit = page.locator(".knowledge-hit").first
    expect(hit).to_contain_text("Tides")
    hit.get_by_role("button", name="Open Tides").click()
    verification = page.locator("#kn-verification")
    expect(verification).to_have_attribute("data-verification", "verified")
    expect(verification.locator(".verification-label")).to_have_text("Verified")
    expect(page.locator("#kn-content")).to_have_text(DOCS["libs/field/tides.md"])
    expect(page.locator("#kn-opened-title")).to_be_focused()

    # Cite the whole revision: Markdown and CSL-JSON come back, claim resolved.
    page.click("#kn-cite-document")
    citation = page.locator("#kn-citation")
    expect(citation.locator(".claim")).to_have_attribute("data-status", "resolved")
    expect(page.locator("#kn-citation-markdown")).to_contain_text("## References")
    expect(page.locator("#kn-citation-csl")).to_contain_text('"verification": "verified"')

    # Quote a selection: byte offsets of the selected words.
    page.evaluate(
        """() => {
          const pre = document.querySelector('#kn-content');
          const node = pre.firstChild;
          const start = pre.textContent.indexOf('spring tides');
          const range = document.createRange();
          range.setStart(node, start);
          range.setEnd(node, start + 'spring tides'.length);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }"""
    )
    page.click("#kn-cite-selection")
    expect(citation.locator(".claim")).to_have_attribute("data-status", "resolved")
    expect(page.locator("#kn-citation-markdown")).to_contain_text("spring tides")

    # The file changes on disk; the cited revision is still served from the
    # evidence store and still verifies, never replaced by the latest text.
    (site.corpus / "libs/field/tides.md").write_bytes(b"# Tides\n\nRewritten.\n")
    citation.get_by_role("button", name="open").first.click()
    expect(page.locator("#kn-opened-title")).to_contain_text("Cited evidence")
    expect(verification).to_have_attribute("data-verification", "verified")
    expect(page.locator("#kn-content")).to_contain_text("spring tides")
    assert page.errors == []  # type: ignore[attr-defined]


def test_unrecorded_revision_is_unsupported_not_substituted(site: Site, page: Any) -> None:
    from playwright.sync_api import expect

    # The snapshot recorded the original text. Change it: the search now finds
    # a current revision the evidence store never recorded. Change it again
    # before opening that hit, so its revision is neither current nor stored.
    (site.corpus / "libs/field/weather.md").write_bytes(b"# Weather\n\nRain and weather.\n")
    _open_tab(page, site, "knowledge")
    _search(page, "weather")
    hit = page.locator(".knowledge-hit").first
    expect(hit).to_contain_text("Weather")
    (site.corpus / "libs/field/weather.md").write_bytes(b"# Weather\n\nSnow and weather.\n")
    hit.get_by_role("button", name="Open Weather").click()
    verification = page.locator("#kn-verification")
    expect(verification).to_have_attribute("data-verification", "unsupported")
    expect(verification.locator(".verification-label")).to_have_text("Unsupported")
    expect(page.locator("#kn-content")).to_have_count(0)
    expect(page.locator("#kn-cite-document")).to_be_hidden()
    assert page.errors == []  # type: ignore[attr-defined]


def test_modules_view_needs_a_key_and_shows_degraded_modules(site: Site, page: Any) -> None:
    from playwright.sync_api import expect

    _open_tab(page, site, "modules")
    expect(page.locator("#mods-needs-key")).to_be_visible()

    host = site.app.state.modules
    host.catalog.set_health("test.probe", "degraded", "unavailable: health probe timed out")
    page.evaluate("key => localStorage.setItem('ke-board-key', key)", site.admin_key)
    page.get_by_role("button", name="Refresh modules").click()
    row = page.locator(".module-row[data-module='test.probe']")
    expect(row).to_have_attribute("data-state", "degraded")
    expect(row.locator(".module-state")).to_have_text(re.compile(r"⚠\s*Degraded"))
    expect(row).to_contain_text("health probe timed out")

    host.catalog.set_health("test.probe", "failed", "process: exited with 2")
    page.get_by_role("button", name="Refresh modules").click()
    expect(row).to_have_attribute("data-state", "failed")
    expect(row.locator(".module-state")).to_have_text(re.compile(r"✕\s*Failed"))

    # The bounded health probe brings it back to ready.
    row.get_by_role("button", name="Check health of test.probe").click()
    expect(row).to_have_attribute("data-state", "ready")
    expect(row.locator(".module-state")).to_have_text(re.compile(r"●\s*Ready"))
    assert page.errors == []  # type: ignore[attr-defined]
