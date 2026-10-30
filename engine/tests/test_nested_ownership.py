"""Nested registry entries: one owner per file in every search mode (issue #39).

Regression fixture from the issue: an enabled library at ``libraries/example``
and enabled entries nested inside it. Before the fix, FTS indexed a nested file
under the parent *and* the child, vector search attributed it to the child only,
and hybrid fusion could return the same file twice.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.core.config import Config
from knowledge_engine.core.registry import Registry, RegistryEntry
from knowledge_engine.project_context.embeddings.providers import FakeProvider
from knowledge_engine.retrieval.embeddings.index import CorpusEmbeddingIndex
from knowledge_engine.retrieval.fts5 import Indexer
from knowledge_engine.retrieval.hybrid import EntryResolver, HybridSearcher
from knowledge_engine.retrieval.ownership import EntryOwnership

FILES = {
    "libraries/example/overview.md": "Parent overview of the quasar catalogue.",
    "libraries/example/research/finding.md": "Zephyrine signal confirmed in the research notes.",
    "libraries/example/skills-inside/how.md": "Marmalite procedure for calibrating the sensor.",
    "libraries/example/archive/old.md": "Obsidianite archive from the disabled child.",
    "libraries/example/single.md": "Tungstenite single file entry text.",
}


FILLER = (
    " This paragraph pads the document so the embedding index treats it as real"
    " content rather than a stub; it repeats neutral words about notes and files."
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[tuple[Registry, Indexer, HybridSearcher]]:
    corpus = tmp_path / "corpus"
    for rel, text in FILES.items():
        (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
        (corpus / rel).write_text(text + FILLER * 3, encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    config = Config(corpus_root=corpus, data_dir=data, registry_path=corpus / "registry.json")
    registry = Registry(config.registry_path)
    for entry in (
        RegistryEntry(id="example", kind="library", name="Example", path="libraries/example"),
        # Same kind, nested.
        RegistryEntry(
            id="research", kind="library", name="Research", path="libraries/example/research"
        ),
        # Different kind, nested.
        RegistryEntry(
            id="inside", kind="skill", name="Inside", path="libraries/example/skills-inside/"
        ),
        # Disabled child: its files fall back to the enclosing library.
        RegistryEntry(
            id="archive",
            kind="library",
            name="Archive",
            path="libraries/example/archive",
            enabled=False,
        ),
        # Single-file entry nested inside the library.
        RegistryEntry(id="single", kind="kit", name="Single", path="libraries/example/single.md"),
    ):
        registry.upsert(entry)
    indexer = Indexer(config, registry)
    indexer.rebuild()
    embeddings = CorpusEmbeddingIndex(
        data / "embeddings.db", FakeProvider(dim=64), corpus_root=corpus
    )
    embeddings.build()
    searcher = HybridSearcher(indexer, registry, embeddings=embeddings)
    try:
        yield registry, indexer, searcher
    finally:
        indexer.close()
        registry.close()


EXPECTED_OWNER = {
    "libraries/example/overview.md": "example",
    "libraries/example/research/finding.md": "research",
    "libraries/example/skills-inside/how.md": "inside",
    "libraries/example/archive/old.md": "example",
    "libraries/example/single.md": "single",
}

QUERIES = {
    "quasar": "libraries/example/overview.md",
    "zephyrine": "libraries/example/research/finding.md",
    "marmalite": "libraries/example/skills-inside/how.md",
    "obsidianite": "libraries/example/archive/old.md",
    "tungstenite": "libraries/example/single.md",
}


def _hits(searcher: HybridSearcher, query: str, mode: str, kind: str | None = None) -> list[Any]:
    return [
        (hit["entry_id"], hit["path"])
        for hit in searcher.search(query, mode=mode, limit=20, kind=kind)
    ]


def test_ownership_rule() -> None:
    ownership = EntryOwnership(
        [
            {"id": "a", "kind": "library", "path": "libraries/a"},
            {"id": "b", "kind": "skill", "path": "libraries/a/b/"},
            {"id": "c", "kind": "library", "path": "libraries/a/b/c", "enabled": False},
            {"id": "d", "kind": "kit", "path": "libraries\\a\\d.md"},
        ]
    )
    assert ownership.owner("libraries/a/x.md")["id"] == "a"  # type: ignore[index]
    assert ownership.owner("libraries/a/b/y.md")["id"] == "b"  # type: ignore[index]
    assert ownership.owner("libraries/a/b/c/z.md")["id"] == "b"  # type: ignore[index]
    assert ownership.owner("libraries/a/d.md")["id"] == "d"  # type: ignore[index]
    assert ownership.owner("libraries/ab/x.md") is None
    assert ownership.owns("b", "libraries/a/b/y.md")
    assert not ownership.owns("a", "libraries/a/b/y.md")


def test_fts_indexes_each_file_once_under_its_owner(
    engine: tuple[Registry, Indexer, HybridSearcher],
) -> None:
    _, indexer, _ = engine
    counts = indexer.rebuild()
    assert counts["files"] == len(FILES)
    rows = indexer._run("SELECT entry_id, relpath FROM docs", [])
    assert sorted((path, entry) for entry, path in rows) == sorted(
        (path, owner) for path, owner in EXPECTED_OWNER.items()
    )


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
@pytest.mark.parametrize("query", sorted(QUERIES))
def test_every_mode_attributes_a_file_to_its_owner_once(
    engine: tuple[Registry, Indexer, HybridSearcher], mode: str, query: str
) -> None:
    _, _, searcher = engine
    path = QUERIES[query]
    hits = [hit for hit in _hits(searcher, query, mode) if hit[1] == path]
    assert hits == [(EXPECTED_OWNER[path], path)]


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
def test_no_mode_returns_a_path_twice(
    engine: tuple[Registry, Indexer, HybridSearcher], mode: str
) -> None:
    _, _, searcher = engine
    for query in QUERIES:
        paths = [path for _, path in _hits(searcher, query, mode)]
        assert len(paths) == len(set(paths)), (mode, query, paths)


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
def test_kind_filter_applies_to_the_owner(
    engine: tuple[Registry, Indexer, HybridSearcher], mode: str
) -> None:
    _, _, searcher = engine
    # The skill nested in a library is a skill, never a library hit.
    assert all(
        path != "libraries/example/skills-inside/how.md"
        for _, path in _hits(searcher, "marmalite", mode, kind="library")
    )
    assert ("inside", "libraries/example/skills-inside/how.md") in _hits(
        searcher, "marmalite", mode, kind="skill"
    )
    # The disabled child's file belongs to the enclosing library.
    assert ("example", "libraries/example/archive/old.md") in _hits(
        searcher, "obsidianite", mode, kind="library"
    )


def test_hybrid_fusion_is_stable(engine: tuple[Registry, Indexer, HybridSearcher]) -> None:
    _, _, searcher = engine
    first = _hits(searcher, "research notes catalogue", "hybrid")
    for _ in range(3):
        assert _hits(searcher, "research notes catalogue", "hybrid") == first


def test_entry_resolver_matches_fts_attribution() -> None:
    entries = [
        {"id": "outer", "kind": "library", "path": "libraries/zoo"},
        {"id": "inner", "kind": "skill", "path": "libraries/zoo/birds/"},
    ]
    # Before the fix a kind filter re-attributed the file to the enclosing entry.
    assert EntryResolver(entries, "library").resolve("libraries/zoo/birds/owl.md") is None
    assert EntryResolver(entries, "skill").resolve("libraries/zoo/birds/owl.md")["id"] == "inner"  # type: ignore[index]


def test_ownership_keys_survive_windows_normcase(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows os.path.normcase lowercases *and* turns "/" into "\\"."""
    monkeypatch.setattr(
        "knowledge_engine.retrieval.ownership.os.path.normcase",
        lambda p: p.replace("/", "\\").lower(),
    )
    ownership = EntryOwnership(
        [
            {"id": "outer", "kind": "library", "path": "Libraries/Zoo"},
            {"id": "inner", "kind": "skill", "path": "libraries/zoo/birds"},
        ]
    )
    assert ownership.owner("libraries/zoo/birds/owl.md")["id"] == "inner"  # type: ignore[index]
    assert ownership.owner("LIBRARIES/ZOO/fox.md")["id"] == "outer"  # type: ignore[index]
    assert ownership.owner("libraries/zoology/x.md") is None
