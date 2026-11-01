"""Vector search backends of the corpus embedding index (``KE_EMBED_BACKEND``).

The ``sqlite-vec`` backend must answer exactly like the ``scan`` backend, keep
its ``vec0`` mirror in sync across builds, and fall back to the scan (never
fail) when the extension or the mirror cannot be used. Offline: vectors come
from the deterministic ``FakeProvider``. Tests that need the extension skip
with a reason when it cannot be loaded on this platform; the fallback tests
run everywhere.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine import cli
from knowledge_engine.core.settings import Settings
from knowledge_engine.project_context.embeddings.providers import EmbeddingProvider, FakeProvider
from knowledge_engine.retrieval.embeddings import backends, errors, index as index_mod
from knowledge_engine.retrieval.embeddings.backends import (
    LOG_TABLE,
    SCAN,
    SQLITE_VEC,
    VEC_TABLE,
    BackendUnavailableError,
    SqliteVecAccelerator,
    parse_backend,
    vec_db_path,
)
from knowledge_engine.retrieval.embeddings.index import CorpusEmbeddingIndex


def _vec_unavailable() -> str | None:
    try:
        backends.SqliteVecStore.probe()
    except BackendUnavailableError as exc:
        return str(exc)
    return None


_VEC_REASON = _vec_unavailable()
needs_vec = pytest.mark.skipif(
    _VEC_REASON is not None, reason=f"sqlite-vec cannot be loaded here: {_VEC_REASON}"
)

_VOCABULARY = (
    "fox owl badger heron river forest snow track paw night flight prey hunt den "
    "burrow meadow winter summer feather fur claw nest egg song call dawn dusk moss "
    "stone pine oak willow reed marsh pond trail scent shadow moon star wind rain"
)
WORDS = tuple(_VOCABULARY.split())
QUERIES = (
    "fox tracks in the winter snow",
    "owl night flight over the marsh",
    "badger den in the oak forest",
    "heron hunting in the pond reeds at dawn",
    "moss stone pine",
)


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - reproducible test text, not security


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(rng: random.Random, title: str) -> str:
    lines = [f"# {title}", ""]
    for section in range(rng.randint(1, 4)):
        lines.append(f"## {title} part {section}")
        lines.extend(
            " ".join(rng.choice(WORDS) for _ in range(rng.randint(6, 16))) + "."
            for _ in range(rng.randint(3, 12))
        )
    return "\n".join(lines) + "\n"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    rng = _rng(907)
    for number in range(60):
        library = ("libraries", "skills", "agents")[number % 3]
        _write(
            root / library / f"group{number % 5}" / f"page{number:02d}.md",
            _page(rng, f"Page {number}"),
        )
    return root


def _index(tmp_path: Path, corpus: Path, backend: str, **kw: Any) -> CorpusEmbeddingIndex:
    return CorpusEmbeddingIndex(
        tmp_path / "data" / "embeddings.db",
        kw.pop("provider", None) or FakeProvider(),
        corpus_root=corpus,
        backend=backend,
        **kw,
    )


def _pairs(tmp_path: Path, corpus: Path) -> tuple[CorpusEmbeddingIndex, CorpusEmbeddingIndex]:
    """A ``scan`` and a ``sqlite-vec`` view of the same index, built with the latter."""
    vec = _index(tmp_path, corpus, SQLITE_VEC, chunk_size=600, chunk_overlap=100)
    stats = vec.build()
    assert stats["backend"]["active"] == SQLITE_VEC, stats["backend"]
    scan = _index(tmp_path, corpus, SCAN, chunk_size=600, chunk_overlap=100)
    return scan, vec


def _key(hits: list[Any]) -> list[tuple[str, int, float]]:
    return [(hit.rel_path, hit.chunk_idx, round(hit.score, 9)) for hit in hits]


def _fallbacks(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "falls back to the scan" in record.getMessage()
        or "answered by the scan" in record.getMessage()
    ]


class _CountingAccelerator(SqliteVecAccelerator):
    calls: list[int] = []  # noqa: RUF012 - reset per test by the fixture

    def nearest(self, query: Any, k: int, library: str | None) -> list[int]:
        type(self).calls.append(k)
        return super().nearest(query, k, library)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    _CountingAccelerator.calls = []
    monkeypatch.setattr(index_mod, "SqliteVecAccelerator", _CountingAccelerator)
    return _CountingAccelerator.calls


# ── parity ───────────────────────────────────────────────────────────


@needs_vec
def test_sqlite_vec_top_k_equals_the_scan(
    tmp_path: Path, corpus: Path, caplog: pytest.LogCaptureFixture, counting: list[int]
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    with caplog.at_level(logging.WARNING):
        for query in QUERIES:
            for top in (1, 5, 17, 500):
                assert _key(vec.search(query, top=top)) == _key(scan.search(query, top=top))
                assert _key(vec.search_files(query, top=top)) == _key(
                    scan.search_files(query, top=top)
                )
            for library in ("skills", "agents", "nope"):
                assert _key(vec.search(query, top=7, library=library)) == _key(
                    scan.search(query, top=7, library=library)
                )
    assert counting, "the sqlite-vec backend was never asked"
    assert _fallbacks(caplog) == []


@needs_vec
def test_accept_filter_applies_before_the_cut_with_refills(
    tmp_path: Path,
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    counting: list[int],
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    # Start tiny so a selective filter needs several refills.
    monkeypatch.setattr(index_mod, "KNN_MIN_FETCH", 2)
    monkeypatch.setattr(index_mod, "KNN_OVERFETCH", 1)
    asked: list[str] = []

    def accept(rel_path: str) -> bool:
        asked.append(rel_path)
        return rel_path.endswith(("7.md", "3.md"))

    with caplog.at_level(logging.WARNING):
        got = vec.search_files(QUERIES[0], top=5, accept=accept)
    want = scan.search_files(QUERIES[0], top=5, accept=accept)
    assert _key(got) == _key(want)
    assert len(got) == 5
    assert all(hit.rel_path.endswith(("7.md", "3.md")) for hit in got)
    assert len(counting) > 1, "expected the backend to be asked again (refill)"
    assert counting == sorted(counting)
    assert _fallbacks(caplog) == []


@needs_vec
def test_reaching_the_cap_answers_with_the_scan_and_warns(
    tmp_path: Path,
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    monkeypatch.setattr(index_mod, "MAX_KNN", 4)
    monkeypatch.setattr(index_mod, "KNN_MIN_FETCH", 1)

    def accept(rel_path: str) -> bool:
        return rel_path.endswith("59.md")

    with caplog.at_level(logging.WARNING):
        got = vec.search_files(QUERIES[1], top=1, accept=accept)
    assert _key(got) == _key(scan.search_files(QUERIES[1], top=1, accept=accept))
    assert [hit.rel_path for hit in got] == ["agents/group4/page59.md"]
    assert any("reached its cap of 4 chunks" in message for message in _fallbacks(caplog))


# ── keeping the mirror in sync ───────────────────────────────────────


def _vec_rows(index: CorpusEmbeddingIndex) -> set[int]:
    with contextlib.closing(sqlite3.connect(index.vec_db_path)) as conn:
        backends.load_sqlite_vec(conn)
        return {int(row[0]) for row in conn.execute(f"SELECT rowid FROM {VEC_TABLE}")}


def _chunk_ids(index: CorpusEmbeddingIndex) -> set[int]:
    with contextlib.closing(sqlite3.connect(index.db_path)) as conn:
        return {int(row[0]) for row in conn.execute("SELECT id FROM embedding_chunks")}


@needs_vec
def test_incremental_builds_keep_the_vec_table_in_sync(tmp_path: Path, corpus: Path) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    assert _vec_rows(vec) == _chunk_ids(vec)

    # Change, add and remove files; the incremental build replays the change log.
    rng = _rng(1)
    _write(corpus / "skills" / "group0" / "page03.md", _page(rng, "Rewritten"))
    _write(corpus / "skills" / "new" / "fresh.md", _page(rng, "Fresh fox owl"))
    (corpus / "agents" / "group2" / "page02.md").unlink()
    stats = vec.build(incremental=True)
    assert stats["backend"]["active"] == SQLITE_VEC
    assert stats["backend"]["sync"]["mode"] == "incremental"
    assert stats["indexed"] == 2
    assert stats["pruned"] == 1
    assert _vec_rows(vec) == _chunk_ids(vec)
    report = vec.status()
    assert report["backend"]["active"] == SQLITE_VEC
    assert report["backend"]["vectors"] == report["chunks"]
    assert report["warnings"] == []
    for query in QUERIES:
        assert _key(vec.search_files(query, top=10)) == _key(scan.search_files(query, top=10))


@needs_vec
def test_a_scan_build_makes_the_mirror_stale_until_the_next_vec_build(
    tmp_path: Path, corpus: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    _write(corpus / "skills" / "new" / "fresh.md", _page(_rng(2), "Fresh"))
    assert scan.build(incremental=True)["backend"] == {"configured": SCAN, "active": SCAN}

    report = vec.status()
    assert report["backend"]["active"] == SCAN
    assert "behind the index" in report["backend"]["warning"]
    assert report["warnings"] and "embeddings build --backend sqlite-vec" in report["warnings"][0]
    with caplog.at_level(logging.WARNING):
        got = vec.search(QUERIES[0], top=5)
    assert _key(got) == _key(scan.search(QUERIES[0], top=5))
    assert any("behind the index" in message for message in _fallbacks(caplog))

    stats = vec.build(incremental=True)
    assert stats["backend"]["sync"]["mode"] == "incremental"
    assert vec.status()["backend"]["active"] == SQLITE_VEC
    assert _vec_rows(vec) == _chunk_ids(vec)


@needs_vec
def test_rebuild_resets_tracking_and_a_vec_rebuild_resyncs_fully(
    tmp_path: Path, corpus: Path
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    scan.build(rebuild=True)  # drops the change log and its triggers
    assert "rebuilt without the sqlite-vec backend" in vec.status()["backend"]["warning"]
    stats = vec.build(incremental=True)
    assert stats["backend"]["sync"]["mode"] == "full"
    assert vec.status()["backend"]["active"] == SQLITE_VEC
    assert _vec_rows(vec) == _chunk_ids(vec)
    with contextlib.closing(sqlite3.connect(vec.db_path)) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {LOG_TABLE}").fetchone()[0] == 0


class _OtherModel(EmbeddingProvider):
    """FakeProvider vectors under another model name."""

    def __init__(self, dim: int = 64) -> None:
        self._inner = FakeProvider(dim=dim)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model_id(self) -> str:
        return "other-model"

    @property
    def dim(self) -> int:
        return self._inner.dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


@needs_vec
def test_mixed_model_refusal_is_unchanged(tmp_path: Path, corpus: Path) -> None:
    _scan, vec = _pairs(tmp_path, corpus)
    before = _vec_rows(vec)
    other = _index(tmp_path, corpus, SQLITE_VEC, provider=_OtherModel())
    with pytest.raises(errors.EmbeddingModelMismatchError):
        other.build(incremental=True)
    with pytest.raises(errors.EmbeddingModelMismatchError):
        other.search("fox")
    with pytest.raises(errors.EmbeddingModelMismatchError):
        other.search_files("fox")
    assert _vec_rows(vec) == before
    assert vec.status()["backend"]["active"] == SQLITE_VEC
    # The supported way to switch models rebuilds the mirror too.
    stats = other.build(rebuild=True)
    assert stats["model"] == "other-model"
    assert stats["backend"]["sync"]["mode"] == "full"
    assert other.status()["backend"]["active"] == SQLITE_VEC


# ── fallback without the extension ───────────────────────────────────


def _unloadable(conn: sqlite3.Connection) -> str:
    raise BackendUnavailableError("the sqlite-vec extension could not be loaded: test")


def test_without_the_package_build_and_search_fall_back_to_the_scan(
    tmp_path: Path,
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # import raises ImportError
    vec = _index(tmp_path, corpus, SQLITE_VEC)
    with caplog.at_level(logging.WARNING):
        stats = vec.build()
    assert stats["backend"]["active"] == SCAN
    assert "not installed" in stats["backend"]["warning"]
    assert "--extra vec" in stats["backend"]["warning"]
    assert not vec.vec_db_path.exists()

    scan = _index(tmp_path, corpus, SCAN)
    with caplog.at_level(logging.WARNING):
        first = vec.search_files(QUERIES[0], top=5)
        second = vec.search(QUERIES[0], top=5)
    assert _key(first) == _key(scan.search_files(QUERIES[0], top=5))
    assert _key(second) == _key(scan.search(QUERIES[0], top=5))
    # Logged once at WARNING per distinct reason, not on every query.
    assert len([m for m in _fallbacks(caplog) if "not installed" in m]) == 1

    report = vec.status()
    assert report["backend"]["configured"] == SQLITE_VEC
    assert report["backend"]["active"] == SCAN
    assert "not installed" in report["backend"]["warning"]
    assert len(report["warnings"]) == 1


@needs_vec
def test_an_unloadable_extension_falls_back_after_a_vec_build(
    tmp_path: Path,
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    monkeypatch.setattr(backends, "load_sqlite_vec", _unloadable)
    with caplog.at_level(logging.WARNING):
        got = vec.search_files(QUERIES[2], top=8)
    assert _key(got) == _key(scan.search_files(QUERIES[2], top=8))
    assert any("could not be loaded: test" in m for m in _fallbacks(caplog))
    report = vec.status()
    assert report["backend"]["active"] == SCAN
    assert "could not be loaded" in report["warnings"][0]


@needs_vec
def test_a_missing_or_broken_vec_table_falls_back(
    tmp_path: Path, corpus: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scan, vec = _pairs(tmp_path, corpus)
    with contextlib.closing(sqlite3.connect(vec.vec_db_path)) as conn:
        backends.load_sqlite_vec(conn)
        conn.execute(f"DROP TABLE {VEC_TABLE}")
        conn.commit()
    with caplog.at_level(logging.WARNING):
        got = vec.search(QUERIES[3], top=4)
    assert _key(got) == _key(scan.search(QUERIES[3], top=4))
    assert any("has not been built" in m for m in _fallbacks(caplog))

    vec.vec_db_path.unlink()
    assert "has not been built" in vec.status()["backend"]["warning"]
    assert vec.build(incremental=True)["backend"]["sync"]["mode"] == "full"
    assert vec.status()["backend"]["active"] == SQLITE_VEC


# ── settings, paths, CLI ─────────────────────────────────────────────


def test_backend_names_and_paths() -> None:
    assert parse_backend(None) == SCAN
    assert parse_backend(" SQLite-Vec ") == SQLITE_VEC
    with pytest.raises(ValueError, match="unknown vector backend"):
        parse_backend("faiss")
    assert vec_db_path(Path("/x/embeddings.db")) == Path("/x/embeddings.vec.db")
    assert vec_db_path(Path("/x/index")) == Path("/x/index.vec.db")


def test_setting_selects_the_backend(
    tmp_path: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KE_EMBED_BACKEND", raising=False)
    assert Settings().embed_backend == SCAN
    monkeypatch.setenv("KE_EMBED_BACKEND", " sqlite-vec ")
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_EMBEDDINGS_DB", str(tmp_path / "e.db"))
    settings = Settings()
    assert settings.embed_backend == SQLITE_VEC
    index = CorpusEmbeddingIndex.from_settings(settings, provider="fake")
    assert index.backend == SQLITE_VEC
    assert index.vec_db_path == (tmp_path / "e.vec.db").resolve()
    assert (
        CorpusEmbeddingIndex.from_settings(settings, provider="fake", backend="scan").backend
        == SCAN
    )
    monkeypatch.setenv("KE_EMBED_BACKEND", "")
    assert Settings().embed_backend == SCAN
    monkeypatch.setenv("KE_EMBED_BACKEND", "annoy")  # invalid: warned about, default used
    assert Settings().embed_backend == SCAN
    with pytest.raises(ValueError, match="unknown vector backend"):
        CorpusEmbeddingIndex(tmp_path / "x.db", FakeProvider(), backend="annoy")


def test_cli_status_reports_the_backend_and_its_warning(
    tmp_path: Path,
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.delenv("KE_EMBED_BACKEND", raising=False)
    db = str(tmp_path / "cli.db")
    code = cli.main(
        ["embeddings", "build", "--provider", "fake", "--db", db, "--backend", "sqlite-vec"]
    )
    assert code == 0
    built = json.loads(capsys.readouterr().out)
    assert built["backend"]["active"] == SCAN
    code = cli.main(
        ["embeddings", "status", "--provider", "fake", "--db", db, "--backend", "sqlite-vec"]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["backend"]["configured"] == SQLITE_VEC
    assert report["backend"]["active"] == SCAN
    assert report["warnings"]
    code = cli.main(["embeddings", "status", "--provider", "fake", "--db", db])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["backend"] == {"configured": SCAN, "active": SCAN}
