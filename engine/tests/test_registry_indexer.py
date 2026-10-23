"""Registry (JSON only, atomic, thread-safe) and Indexer (lock scope, syntax fallback)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.core.config import Config
from knowledge_engine.core.registry import (
    BUCKET_TO_KIND,
    CORPUS_FOLDER_TO_KIND,
    KIND_TO_BUCKET,
    Registry,
    RegistryClosedError,
    RegistryEntry,
)
from knowledge_engine.retrieval.fts5 import Indexer, SearchResults, quote_phrase


def _config(tmp_path: Path) -> Config:
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Config(corpus_root=corpus, data_dir=data, registry_path=corpus / "registry.json")


def _entry(n: int, kind: Any = "kit") -> RegistryEntry:
    return RegistryEntry(id=f"{kind}-{n}", kind=kind, name=f"E{n}", path=f"kits/e{n}")


# ── Registry ─────────────────────────────────────────────────────────


def test_registry_writes_only_json(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = Registry(config.registry_path, config.data_dir / "registry.db")
    registry.upsert(_entry(1))
    assert not (config.data_dir / "registry.db").exists()
    doc = json.loads(config.registry_path.read_text(encoding="utf-8"))
    assert [row["id"] for row in doc["kits"]] == ["kit-1"]
    assert list((config.registry_path.parent).glob(".registry.json.*.tmp")) == []


def test_registry_counts_and_maps(tmp_path: Path) -> None:
    registry = Registry(_config(tmp_path).registry_path)
    registry.upsert_many([_entry(1), _entry(2), _entry(3, "library")])
    assert registry.counts() == {"libraries": 1, "skills": 0, "kits": 2, "tools": 0}
    assert {v: k for k, v in KIND_TO_BUCKET.items()} == BUCKET_TO_KIND
    assert CORPUS_FOLDER_TO_KIND["capabilities"] == "tool"
    assert registry.upsert_many([]) == []


def test_registry_concurrent_toggles_from_eight_threads(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = Registry(config.registry_path)
    registry.upsert_many([_entry(n) for n in range(8)])
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def toggle(worker: int) -> None:
        try:
            barrier.wait()
            for round_ in range(20):
                registry.set_enabled(f"kit-{(worker + round_) % 8}", round_ % 2 == 0)
                registry.list(enabled_only=True)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=toggle, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    on_disk = json.loads(config.registry_path.read_text(encoding="utf-8"))
    assert sorted(row["id"] for row in on_disk["kits"]) == [f"kit-{n}" for n in range(8)]
    # Memory and disk agree after the storm.
    assert {r["id"]: r["enabled"] for r in registry.list()} == {
        row["id"]: row["enabled"] for row in on_disk["kits"]
    }
    assert Registry(config.registry_path).list() == registry.list()


def test_failed_write_leaves_memory_and_file_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    registry = Registry(config.registry_path)
    registry.upsert(_entry(1))
    before_file = config.registry_path.read_text(encoding="utf-8")

    def disk_full(_data: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(registry, "_write_json", disk_full)
    with pytest.raises(OSError, match="No space"):
        registry.set_enabled("kit-1", False)
    with pytest.raises(OSError):
        registry.upsert(_entry(2))
    assert registry.get("kit-1")["enabled"] is True  # type: ignore[index]
    assert registry.get("kit-2") is None
    assert config.registry_path.read_text(encoding="utf-8") == before_file


def test_missing_ids_do_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry(_config(tmp_path).registry_path)
    writes: list[int] = []
    monkeypatch.setattr(registry, "_write_json", lambda _d: writes.append(1))
    toggled = registry.set_enabled("nope", True)
    removed = registry.remove("nope")
    assert toggled is None
    assert removed is False
    assert writes == []


def test_closed_registry_refuses_mutations(tmp_path: Path) -> None:
    registry = Registry(_config(tmp_path).registry_path)
    registry.upsert(_entry(1))
    registry.close()
    registry.close()
    assert registry.closed
    with pytest.raises(RegistryClosedError):
        registry.set_enabled("kit-1", False)
    with pytest.raises(sqlite3.ProgrammingError):  # historical exception type still matches
        registry.set_lifecycle(watch_enabled=False)
    assert registry.get("kit-1") is not None  # reads keep working


def test_non_object_registry_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.registry_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        Registry(config.registry_path)


def test_reload_picks_up_out_of_band_edits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = Registry(config.registry_path)
    doc = json.loads(config.registry_path.read_text(encoding="utf-8"))
    doc["skills"].append({"id": "skill-x", "name": "X", "path": "skills/x"})
    config.registry_path.write_text(json.dumps(doc), encoding="utf-8")
    assert registry.get("skill-x") is None
    registry.reload()
    assert registry.get("skill-x") == {
        "id": "skill-x",
        "name": "X",
        "path": "skills/x",
        "kind": "skill",
    }


# ── corpus folder registration (bootstrap / watcher) ─────────────────


def test_register_corpus_folders_counts_sorted_and_single_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from knowledge_engine.core import watcher

    config = _config(tmp_path)
    for rel in ("libraries/Zeta", "libraries/alpha", "libraries/.hidden", "capabilities/My Tool"):
        (config.corpus_root / rel).mkdir(parents=True)
    (config.corpus_root / "libraries" / "file.md").write_text("x", encoding="utf-8")
    # Two folders with the same slug ("beta-gamma"); distinct names on
    # case-insensitive filesystems too, unlike "alpha" vs "ALPHA".
    (config.corpus_root / "libraries" / "beta gamma").mkdir()
    (config.corpus_root / "libraries" / "beta-gamma").mkdir()
    registry = Registry(config.registry_path)
    writes: list[int] = []
    real_write = registry._write_json

    def counting(data: dict[str, Any]) -> None:
        writes.append(1)
        real_write(data)

    monkeypatch.setattr(registry, "_write_json", counting)
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.watcher"):
        counts = watcher.register_corpus_folders(config, registry)
    # Two folders share the slug "beta-gamma": as before the refactor the later
    # one (in sorted order) wins and both count as added; the collision is logged.
    assert counts == {"library": 4, "skill": 0, "kit": 0, "tool": 1, "skipped": 0}
    assert writes == [1]
    # Insertion order follows the sorted directory walk ("Zeta" sorts before
    # the lowercase names); the replaced duplicate keeps its slot.
    assert [e["id"] for e in registry.list("library")] == [
        "library-zeta",
        "library-alpha",
        "library-beta-gamma",
    ]
    assert registry.get("library-beta-gamma")["name"] == "beta-gamma"  # type: ignore[index]
    assert "duplicate corpus slug 'library-beta-gamma'" in caplog.text
    assert registry.get("tool-my-tool")["path"] == "capabilities/My Tool"  # type: ignore[index]

    changed: list[int] = []
    added = watcher.auto_register(config, registry, lambda: changed.append(1))
    assert added == 0
    assert changed == []
    (config.corpus_root / "skills" / "new").mkdir(parents=True)
    added = watcher.auto_register(config, registry, lambda: changed.append(1))
    assert added == 1
    assert changed == [1]


# ── Indexer ──────────────────────────────────────────────────────────


@pytest.fixture
def indexed(tmp_path: Path) -> tuple[Indexer, Registry]:
    config = _config(tmp_path)
    lib = config.corpus_root / "libraries" / "zoo"
    lib.mkdir(parents=True)
    (lib / "fox.md").write_text("The quick brown fox jumps over the lazy dog.", encoding="utf-8")
    (lib / "c-plus.md").write_text("Notes on c++ templates and foo-bar names.", encoding="utf-8")
    registry = Registry(config.registry_path)
    registry.upsert(RegistryEntry(id="zoo", kind="library", name="Zoo", path="libraries/zoo"))
    indexer = Indexer(config, registry)
    indexer.rebuild()
    yield indexer, registry
    indexer.close()


def test_index_uses_standard_pragmas(indexed: tuple[Indexer, Registry]) -> None:
    indexer, _ = indexed
    conn = sqlite3.connect(indexer.db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_valid_query_has_no_fallback(indexed: tuple[Indexer, Registry]) -> None:
    indexer, _ = indexed
    hits = indexer.search("fox")
    assert isinstance(hits, SearchResults)
    assert isinstance(hits, list)
    assert [h["path"] for h in hits] == ["libraries/zoo/fox.md"]
    assert hits.syntax_fallback is False
    assert hits.meta == {}


@pytest.mark.parametrize("query", ['"unbalanced', "foo-bar", "c++", "AND"])
def test_syntax_error_retries_as_phrase(
    indexed: tuple[Indexer, Registry], query: str, caplog: pytest.LogCaptureFixture
) -> None:
    indexer, _ = indexed
    with caplog.at_level(logging.DEBUG, logger="knowledge_engine.retrieval.fts5"):
        hits = indexer.search(query)
    assert hits.syntax_fallback is True
    assert hits.meta == {"syntax_fallback": True}
    assert "retrying as a phrase" in caplog.text


def test_phrase_fallback_finds_hyphenated_terms(indexed: tuple[Indexer, Registry]) -> None:
    indexer, _ = indexed
    hits = indexer.search("foo-bar")
    assert hits.syntax_fallback is True
    assert [h["path"] for h in hits] == ["libraries/zoo/c-plus.md"]


def test_quote_phrase_escapes_quotes() -> None:
    assert quote_phrase('say "hi"') == '"say ""hi"""'


def test_broken_index_propagates_instead_of_claiming_a_syntax_fallback(
    indexed: tuple[Indexer, Registry],
) -> None:
    indexer, _ = indexed
    with indexer._lock:
        indexer._conn.execute("DROP TABLE docs")
    # Not a query problem: the error reaches the caller (the HTTP layer maps
    # sqlite3.OperationalError to 503) rather than an empty "fallback" result.
    with pytest.raises(sqlite3.OperationalError):
        indexer.search("fox")


def test_unparseable_query_after_phrase_retry_is_logged_not_silent(
    indexed: tuple[Indexer, Registry],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexer, _ = indexed
    calls: list[list[Any]] = []

    def always_syntax_error(sql: str, params: list[Any]) -> list[Any]:
        calls.append(list(params))
        raise sqlite3.OperationalError('fts5: syntax error near "*"')

    monkeypatch.setattr(indexer, "_run", always_syntax_error)
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.retrieval.fts5"):
        hits = indexer.search("fox AND")
    assert hits == []
    assert hits.syntax_fallback is True
    assert len(calls) == 2
    assert "FTS5 search failed" in caplog.text


def test_rebuild_reads_corpus_before_taking_the_lock(
    indexed: tuple[Indexer, Registry], monkeypatch: pytest.MonkeyPatch
) -> None:
    indexer, _ = indexed
    lock_free_during_reads: list[bool] = []
    real_read = Path.read_text

    def spying_read(self: Path, *args: Any, **kwargs: Any) -> str:
        acquired = indexer._lock.acquire(blocking=False)
        if acquired:
            indexer._lock.release()
        lock_free_during_reads.append(acquired)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spying_read)
    counts = indexer.rebuild()
    assert counts == {"entries": 1, "files": 2, "missing": 0}
    assert lock_free_during_reads
    assert all(lock_free_during_reads)


def test_search_during_rebuild_sees_whole_index(indexed: tuple[Indexer, Registry]) -> None:
    indexer, _ = indexed
    stop = threading.Event()
    seen: list[int] = []

    def searcher() -> None:
        while not stop.is_set():
            seen.append(len(indexer.search("fox")))

    thread = threading.Thread(target=searcher)
    thread.start()
    try:
        for _ in range(10):
            indexer.rebuild()
    finally:
        stop.set()
        thread.join()
    assert seen
    assert set(seen) == {1}


def test_rebuild_and_search_after_close_raise(indexed: tuple[Indexer, Registry]) -> None:
    indexer, _ = indexed
    indexer.close()
    indexer.close()
    with pytest.raises(sqlite3.ProgrammingError):
        indexer.rebuild()
    with pytest.raises(sqlite3.ProgrammingError):
        indexer.search("fox")


def test_http_search_reports_syntax_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from knowledge_engine.app import create_app
    from knowledge_engine.core.settings import Settings

    config = _config(tmp_path)
    monkeypatch.setenv("KE_HOME", str(tmp_path))
    settings = Settings(
        corpus_root=str(config.corpus_root),
        data_dir=str(config.data_dir),
        registry_path=str(config.registry_path),
        board_enabled=False,
        provider_startup_probe=False,
    )
    with TestClient(create_app(settings)) as client:
        normal = client.get("/search", params={"q": "fox"}).json()
        assert "syntax_fallback" not in normal
        broken = client.get("/search", params={"q": '"unbalanced'}).json()
        assert broken["syntax_fallback"] is True
        assert broken["count"] == len(broken["results"])
