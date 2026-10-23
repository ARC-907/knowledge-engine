"""Corpus embedding index (``retrieval.embeddings``): build, search, refusal, packing.

Offline: every vector comes from the deterministic ``FakeProvider`` (or a small
in-test provider); no Ollama or HTTP endpoint is contacted.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from knowledge_engine.core.settings import Settings
from knowledge_engine.core.util.vectors import (
    VECTOR_FORMAT_VERSION,
    CosineScorer,
    VectorDimensionError,
    cosine,
    pack_vector,
    unpack_vector,
)
from knowledge_engine.project_context.embeddings.providers import (
    EmbeddingProvider,
    FakeProvider,
    StubProvider,
)
from knowledge_engine.retrieval.embeddings import errors, index as index_mod
from knowledge_engine.retrieval.embeddings.index import (
    CorpusEmbeddingIndex,
    chunk_text,
    same_model,
)

FOX = "# Foxes\n\nThe quick red fox jumps over the lazy dog. Foxes are small omnivorous mammals.\n"
OWL = "# Owls\n\nOwls are nocturnal birds of prey with excellent night vision and silent flight.\n"
TRACK = "# Tracking\n\nHow to track a fox in the snow: look for small paw prints in a line.\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    _write(root / "libraries" / "zoo" / "fox.md", FOX)
    _write(root / "libraries" / "zoo" / "owl.md", OWL)
    _write(root / "skills" / "track" / "t.md", TRACK)
    _write(root / "libraries" / "zoo" / "tiny.md", "too short")  # skipped (< 50 chars)
    _write(root / ".git" / "x.md", FOX)  # skipped (dot directory)
    _write(root / "node_modules" / "pkg" / "x.md", FOX)  # skipped (SKIP_DIRS)
    return root


def _index(tmp_path: Path, corpus: Path, provider: EmbeddingProvider | None = None, **kw: Any):
    return CorpusEmbeddingIndex(
        tmp_path / "data" / "embeddings.db", provider or FakeProvider(), corpus_root=corpus, **kw
    )


class CountingProvider(EmbeddingProvider):
    """FakeProvider vectors under a configurable model name, recording each call."""

    def __init__(self, model: str = "m1", dim: int = 16) -> None:
        self._inner = FakeProvider(dim=dim)
        self._model = model
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return f"counting:{self._model}"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return 0  # discovered from the vectors, like Ollama

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._inner.embed(texts)


# ── build → search ───────────────────────────────────────────────────


def test_build_then_search_round_trip(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    stats = index.build()
    assert stats["indexed"] == 3
    assert stats["skipped"] == 1
    assert stats["chunks"] == 3
    assert stats["errors"] == 0
    assert (stats["provider"], stats["model"], stats["dim"]) == ("fake", "fake-hash-64", 64)

    hits = index.search("fox paw prints in the snow", top=2)
    assert [hit.rel_path for hit in hits] == ["skills/track/t.md", "libraries/zoo/fox.md"]
    assert hits[0].score > hits[1].score
    assert hits[0].heading == "Tracking"
    assert hits[0].text == TRACK
    assert hits[0].library == "skills"
    assert set(hits[0].to_dict()) == {
        "score",
        "rel_path",
        "library",
        "heading",
        "text",
        "chunk_idx",
    }

    # An exact chunk text is its own nearest neighbour.
    [best] = index.search(OWL, top=1)
    assert best.rel_path == "libraries/zoo/owl.md"
    assert math.isclose(best.score, 1.0, rel_tol=1e-6)


def test_search_library_filter_and_validation(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    index.build()
    hits = index.search("fox", top=10, library="skills")
    assert [hit.rel_path for hit in hits] == ["skills/track/t.md"]
    with pytest.raises(ValueError):
        index.search("fox", top=0)
    with pytest.raises(ValueError):
        index.search("   ")


def test_search_streams_rows_in_pages(
    tmp_path: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(12):
        _write(corpus / "libraries" / "many" / f"n{n}.md", f"# Note {n}\n\n" + "filler words " * 10)
    index = _index(tmp_path, corpus)
    index.build()
    full = [hit.rel_path for hit in index.search("fox jumps", top=4)]
    # A page size far below the row count gives the same answer.
    monkeypatch.setattr(index_mod, "SCAN_PAGE", 2)
    assert [hit.rel_path for hit in index.search("fox jumps", top=4)] == full
    files = index.search_files("note filler", top=3)
    assert len({hit.rel_path for hit in files}) == 3


def test_search_files_returns_one_hit_per_file_and_applies_accept(
    tmp_path: Path, corpus: Path
) -> None:
    long_fox = "\n".join(f"fox line {n} " + "x" * 60 for n in range(60))
    _write(corpus / "libraries" / "zoo" / "long.md", "# Long\n" + long_fox)
    index = _index(tmp_path, corpus, chunk_size=400, chunk_overlap=50)
    index.build()
    assert len([h for h in index.search("fox line", top=50) if h.rel_path.endswith("long.md")]) > 1
    files = index.search_files("fox line", top=10)
    paths = [hit.rel_path for hit in files]
    assert len(paths) == len(set(paths))
    only_skills = index.search_files("fox", top=10, accept=lambda rel: rel.startswith("skills/"))
    assert [hit.rel_path for hit in only_skills] == ["skills/track/t.md"]


# ── incremental, pruning, chunking ──────────────────────────────────


def test_incremental_skips_unchanged_and_prunes_removed(tmp_path: Path, corpus: Path) -> None:
    provider = CountingProvider()
    index = _index(tmp_path, corpus, provider)
    index.build()
    provider.calls.clear()

    again = index.build(incremental=True)
    assert again["indexed"] == 0
    assert provider.calls == []

    _write(corpus / "libraries" / "zoo" / "fox.md", FOX + "\nFoxes also like berries.\n")
    (corpus / "libraries" / "zoo" / "owl.md").unlink()
    changed = index.build(incremental=True)
    assert changed["indexed"] == 1
    assert changed["pruned"] == 1
    status = index.status()
    assert status["files"] == 2
    assert [row["library"] for row in status["libraries"]] == ["libraries", "skills"]


def test_batches_span_files(tmp_path: Path, corpus: Path) -> None:
    provider = CountingProvider()
    index = _index(tmp_path, corpus, provider, batch_size=2)
    index.build()
    # Three one-chunk files at batch size 2: one call of 2 texts, then one of 1.
    assert [len(call) for call in provider.calls] == [2, 1]


def test_max_embed_chars_truncates_what_is_embedded_not_what_is_stored(
    tmp_path: Path, corpus: Path
) -> None:
    provider = CountingProvider()
    index = _index(tmp_path, corpus, provider, max_embed_chars=20)
    index.build()
    assert all(len(text) <= 20 for call in provider.calls for text in call)
    [hit] = index.search(FOX, top=1)
    assert len(hit.text) > 20


def test_changed_chunking_reembeds_everything(
    tmp_path: Path, corpus: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _index(tmp_path, corpus).build()
    caplog.set_level(logging.WARNING, logger="knowledge_engine.retrieval.embeddings.index")
    stats = _index(tmp_path, corpus, chunk_size=500, chunk_overlap=50).build(incremental=True)
    assert stats["indexed"] == 3
    assert "chunking parameters changed" in caplog.text
    info = _index(tmp_path, corpus).info()
    assert info is not None and info.chunk_size == 500


def test_chunk_text_keeps_the_historical_behaviour() -> None:
    text = "\n".join(["# Title", *[f"line {n} " + "y" * 90 for n in range(40)]])
    chunks = chunk_text(text, "a/b.md")
    assert len(chunks) > 1
    assert all(chunk.heading == "Title" for chunk in chunks)
    # The next chunk starts with the trailing lines (>= 200 chars) of the previous one.
    first_lines = chunks[0].text.split("\n")
    second_lines = chunks[1].text.split("\n")
    overlap = first_lines[first_lines.index(second_lines[0]) :]
    assert second_lines[: len(overlap)] == overlap
    assert sum(len(line) + 1 for line in overlap) >= 200
    assert all(len(chunk.text) >= 1500 for chunk in chunks[:-1])
    assert chunk_text("no heading here", "x.md")[0].heading == "x.md"


@pytest.mark.parametrize(
    "kwargs",
    [{"batch_size": 0}, {"chunk_size": 0}, {"chunk_overlap": 1500}, {"max_embed_chars": 0}],
)
def test_constructor_validates(tmp_path: Path, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        CorpusEmbeddingIndex(tmp_path / "e.db", FakeProvider(), **kwargs)


def test_build_requires_an_existing_corpus(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="corpus root"):
        _index(tmp_path, tmp_path / "missing").build()
    with pytest.raises(ValueError, match="corpus_root"):
        CorpusEmbeddingIndex(tmp_path / "e.db", FakeProvider()).build()


# ── refusing mixed models ────────────────────────────────────────────


def test_build_refuses_another_model(tmp_path: Path, corpus: Path) -> None:
    _index(tmp_path, corpus, CountingProvider("m1")).build()
    other = _index(tmp_path, corpus, CountingProvider("m2"))
    with pytest.raises(errors.EmbeddingModelMismatchError) as caught:
        other.build()
    exc = caught.value
    assert exc.code == "embedding_model_mismatch"
    assert exc.status_code == 409
    assert "--rebuild" in exc.hint
    assert exc.details["index_model"] == "m1"
    assert exc.details["model"] == "m2"
    body = exc.to_dict()
    assert set(body) == {"type", "detail", "hint", "embedding"}
    # Nothing of the other model was written; --rebuild switches models.
    assert other.status()["model"] == "m1"
    assert other.build(rebuild=True)["model"] == "m2"


def test_build_refuses_vectors_of_another_dimension(tmp_path: Path, corpus: Path) -> None:
    _index(tmp_path, corpus, CountingProvider("m1", dim=16)).build()
    _write(corpus / "libraries" / "zoo" / "new.md", OWL + " new")
    with pytest.raises(errors.EmbeddingModelMismatchError, match="16-dimensional"):
        _index(tmp_path, corpus, CountingProvider("m1", dim=8)).build(incremental=True)


def test_mismatch_hint_names_the_provider_only_when_it_differs(
    tmp_path: Path, corpus: Path
) -> None:
    db = tmp_path / "embeddings.db"
    CorpusEmbeddingIndex(db, FakeProvider(dim=8), corpus_root=corpus).build()
    with pytest.raises(errors.EmbeddingModelMismatchError) as same_provider:
        CorpusEmbeddingIndex(db, FakeProvider(dim=16), corpus_root=corpus).search("x")
    assert "KE_EMBED_PROVIDER" not in same_provider.value.hint
    assert "KE_EMBED_MODEL=fake-hash-8" in same_provider.value.hint
    with pytest.raises(errors.EmbeddingModelMismatchError) as other_provider:
        CorpusEmbeddingIndex(db, StubProvider(dim=8), corpus_root=corpus).search("x")
    assert "KE_EMBED_PROVIDER=fake KE_EMBED_MODEL=fake-hash-8" in other_provider.value.hint
    assert other_provider.value.details["index_provider"] == "fake"
    public = other_provider.value.to_dict(public=True)
    assert "db_path" not in public["embedding"]
    assert str(db) not in public["detail"]
    assert other_provider.value.to_dict()["embedding"]["db_path"] == str(db)


def test_query_with_another_model_is_refused(tmp_path: Path, corpus: Path) -> None:
    _index(tmp_path, corpus, CountingProvider("m1")).build()
    with pytest.raises(errors.EmbeddingModelMismatchError):
        _index(tmp_path, corpus, CountingProvider("m2")).search("fox")


def test_ollama_latest_tag_is_the_same_model() -> None:
    assert same_model("bge-m3", "bge-m3:latest")
    assert same_model(" bge-m3:latest", "bge-m3")
    assert not same_model("bge-m3", "bge-m3:567m")
    assert not same_model("nomic-embed-text", "bge-m3")


def test_corrupt_vector_of_another_dimension_is_refused(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    index.build()
    with sqlite3.connect(index.db_path) as conn:
        conn.execute(
            "UPDATE embedding_chunks SET embedding = ? WHERE id = 1", (pack_vector([1.0]),)
        )
    with pytest.raises(errors.EmbeddingModelMismatchError):
        index.search("fox")


# ── missing / legacy index, status ──────────────────────────────────


def test_missing_index_is_a_structured_error(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    with pytest.raises(errors.EmbeddingIndexMissingError) as caught:
        index.search("fox")
    assert isinstance(caught.value, FileNotFoundError)  # the historical contract
    assert "embeddings build" in caught.value.hint
    assert not index.db_path.exists()  # searching never creates the database
    assert index.status() == {
        "db_path": str(index.db_path),
        "exists": False,
        "built": False,
        "configured": {"provider": "fake", "model": "fake-hash-64"},
    }


def test_legacy_tables_are_reported_and_dropped_by_rebuild(tmp_path: Path, corpus: Path) -> None:
    db = tmp_path / "data" / "embeddings.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, rel_path TEXT)")
        conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, embedding BLOB)")
    index = _index(tmp_path, corpus)
    with pytest.raises(errors.EmbeddingIndexMissingError, match="older release") as caught:
        index.search("fox")
    assert "--rebuild" in caught.value.hint
    assert index.status()["legacy_tables"] == ["chunks", "files"]
    index.build(rebuild=True)
    assert index.status()["legacy_tables"] == []


def test_status_reports_the_build(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus)
    index.build()
    status = index.status()
    assert status["built"] is True
    assert status["compatible"] is True
    assert (status["model"], status["dim"], status["vector_format"]) == (
        "fake-hash-64",
        64,
        VECTOR_FORMAT_VERSION,
    )
    assert (status["chunk_size"], status["chunk_overlap"], status["max_embed_chars"]) == (
        1500,
        200,
        2000,
    )
    assert status["files"] == 3 and status["chunks"] == 3
    assert status["created_at"] <= status["updated_at"]
    assert _index(tmp_path, corpus, CountingProvider("other")).status()["compatible"] is False


# ── provider failures ────────────────────────────────────────────────


class FlakyProvider(CountingProvider):
    """Fails any batch larger than one text, and any text containing ``owl``."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        if len(texts) > 1 or "Owls" in texts[0]:
            raise RuntimeError("upstream returned HTTP 500")
        return super().embed(texts)


class DownProvider(CountingProvider):
    """Every request fails at the transport level (connection refused)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        self.calls.append(list(texts))
        raise RuntimeError("request failed") from httpx.ConnectError("refused")


def test_a_failing_chunk_is_skipped_and_retried_next_time(tmp_path: Path, corpus: Path) -> None:
    index = _index(tmp_path, corpus, FlakyProvider())
    stats = index.build()
    assert stats["errors"] == 1
    assert stats["chunks"] == 2
    assert index.status()["incomplete_files"] == 1
    # The incomplete file has no recorded hash, so --incremental retries it.
    retry = _index(tmp_path, corpus, CountingProvider()).build(incremental=True)
    assert retry["indexed"] == 1


def test_an_unreachable_provider_aborts_with_a_structured_error(
    tmp_path: Path, corpus: Path
) -> None:
    provider = DownProvider()
    with pytest.raises(errors.EmbeddingProviderError) as caught:
        _index(tmp_path, corpus, provider).build()
    assert caught.value.status_code == 502
    assert caught.value.code == "embedding_provider_error"
    # One failed batch, no per-chunk retry storm against a server that is down.
    assert len(provider.calls) == 1

    _index(tmp_path, corpus, CountingProvider()).build(rebuild=True)
    with pytest.raises(errors.EmbeddingProviderError):
        _index(tmp_path, corpus, DownProvider()).search("fox")


def test_one_unembeddable_chunk_does_not_abort_a_running_build(
    tmp_path: Path, corpus: Path
) -> None:
    # Batch size 1: fox.md succeeds first, then owl.md fails on its own.
    stats = _index(tmp_path, corpus, FlakyProvider(), batch_size=1).build()
    assert (stats["indexed"], stats["chunks"], stats["errors"]) == (3, 2, 1)


def test_a_build_whose_first_batch_fails_entirely_is_aborted(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _write(root / "a" / "owl.md", OWL)
    with pytest.raises(errors.EmbeddingProviderError):
        _index(tmp_path, root, FlakyProvider()).build()


def test_an_empty_index_adopts_a_new_model(tmp_path: Path, corpus: Path) -> None:
    with pytest.raises(errors.EmbeddingProviderError):
        _index(tmp_path, corpus, DownProvider("typo-model")).build()
    info = _index(tmp_path, corpus).info()
    assert info is not None and info.model == "typo-model"
    # Nothing was stored, so fixing the model needs no --rebuild.
    stats = _index(tmp_path, corpus, CountingProvider("m1")).build()
    assert (stats["model"], stats["chunks"]) == ("m1", 3)


# ── settings / factory ───────────────────────────────────────────────


def test_from_settings_uses_paths_and_embedding_settings(tmp_path: Path, corpus: Path) -> None:
    settings = Settings(
        corpus_root=str(corpus),
        data_dir=str(tmp_path / "data"),
        embed_provider="fake",
        embed_batch=7,
    )
    index = CorpusEmbeddingIndex.from_settings(settings)
    assert index.db_path == (tmp_path / "data" / "embeddings.db").resolve()
    assert index.corpus_root == corpus.resolve()
    assert index.batch_size == 7
    assert index.provider.name == "fake"
    explicit = CorpusEmbeddingIndex.from_settings(
        Settings(embeddings_db=str(tmp_path / "x.db"), embed_provider="fake")
    )
    assert explicit.db_path == (tmp_path / "x.db").resolve()


def test_factory_builds_each_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.project_context.embeddings.local import LocalOllamaProvider
    from knowledge_engine.project_context.embeddings.remote import RemoteProvider
    from knowledge_engine.retrieval.embeddings.factory import EMBED_TIMEOUT, build_provider

    ollama = build_provider(Settings(ollama_url="http://gpu:11434", embed_batch=5))
    assert isinstance(ollama, LocalOllamaProvider)
    assert (ollama.model_id, ollama.base_url, ollama.batch_size) == (
        "bge-m3:latest",
        "http://gpu:11434",
        5,
    )
    assert ollama._client.timeout == EMBED_TIMEOUT
    assert build_provider(Settings(), provider="fake").name == "fake"
    assert build_provider(Settings(), model="nomic").model_id == "nomic"

    with pytest.raises(errors.EmbeddingUnavailableError, match="disabled"):
        build_provider(Settings(embed_provider="remote", embed_remote_url="http://r/"))
    with pytest.raises(errors.EmbeddingUnavailableError, match="endpoint"):
        build_provider(Settings(embed_provider="remote", embed_allow_remote=True))
    remote = build_provider(
        Settings(embed_provider="remote", embed_allow_remote=True, embed_remote_url="http://r/")
    )
    assert isinstance(remote, RemoteProvider)
    with pytest.raises(errors.EmbeddingUnavailableError, match="unknown"):
        build_provider(Settings(), provider="nope")


def test_embedding_settings_parse_leniently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KE_EMBED_PROVIDER", " FAKE ")
    monkeypatch.setenv("KE_EMBED_BATCH", "0")
    monkeypatch.setenv("KE_EMBED_MODEL", "  ")
    monkeypatch.setenv("KE_EMBED_ALLOW_REMOTE", "yes")
    s = Settings()
    assert s.embed_provider == "fake"
    assert s.embed_batch == 32  # invalid -> default, with a warning
    assert s.embed_model == "bge-m3:latest"
    assert s.embed_allow_remote is True
    monkeypatch.setenv("KE_EMBED_PROVIDER", "mystery")
    caplog.set_level(logging.WARNING)
    assert Settings().embed_provider == "ollama"
    assert "KE_EMBED_PROVIDER" in caplog.text


# ── legacy module surface ────────────────────────────────────────────


def test_legacy_build_and_search_modules_keep_working(
    tmp_path: Path,
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from knowledge_engine.retrieval import embeddings
    from knowledge_engine.retrieval.embeddings import build, search

    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KE_EMBED_PROVIDER", "fake")
    assert (tmp_path / "data" / "embeddings.db").resolve() == build.DB_PATH
    assert corpus.resolve() == build.CORPUS_ROOT
    assert build.EMBED_MODEL == "bge-m3:latest"
    with pytest.raises(FileNotFoundError):
        search.search("fox")
    build.show_stats()
    assert "No embedding index" in capsys.readouterr().out
    assert embeddings.build_index()["indexed"] == 3
    hits = search.search("fox paw prints", top=1)
    assert hits[0]["rel_path"] == "skills/track/t.md"
    build.show_stats()
    assert "Total chunks: 3" in capsys.readouterr().out
    assert embeddings.blob_to_embedding(embeddings.embedding_to_blob([1.0, 2.0])) == [1.0, 2.0]
    with pytest.raises(VectorDimensionError):
        embeddings.cosine_similarity([1.0, 0.0], [1.0])
    with pytest.raises(AttributeError):
        _ = build.NOT_A_SETTING


# ── vector helpers ───────────────────────────────────────────────────

_FLOAT32 = st.floats(allow_nan=False, allow_infinity=False, width=32)


@hyp_settings(max_examples=200, deadline=None)
@given(st.lists(_FLOAT32, max_size=64))
def test_pack_unpack_round_trip_property(vector: list[float]) -> None:
    blob = pack_vector(vector)
    assert blob[0] == VECTOR_FORMAT_VERSION
    assert len(blob) == 1 + 4 * len(vector)
    assert unpack_vector(blob) == vector  # float32 values survive exactly


@hyp_settings(max_examples=100, deadline=None)
@given(st.lists(_FLOAT32, min_size=1, max_size=32))
def test_legacy_blob_reads_like_the_versioned_one(vector: list[float]) -> None:
    import struct

    legacy = struct.pack(f"<{len(vector)}f", *vector)
    assert unpack_vector(legacy) == unpack_vector(pack_vector(vector)) == vector


@hyp_settings(max_examples=100, deadline=None)
@given(
    st.lists(st.floats(-1e3, 1e3), min_size=1, max_size=16),
    st.lists(st.floats(-1e3, 1e3), min_size=1, max_size=16),
)
def test_cosine_is_bounded_symmetric_and_strict(a: list[float], b: list[float]) -> None:
    if len(a) != len(b):
        with pytest.raises(VectorDimensionError):
            cosine(a, b)
        with pytest.raises(VectorDimensionError):
            CosineScorer(a)(b)
        return
    value = cosine(a, b)
    assert -1.0 - 1e-9 <= value <= 1.0 + 1e-9
    assert math.isclose(value, cosine(b, a), abs_tol=1e-12)
    assert math.isclose(CosineScorer(a)(b), value, abs_tol=1e-12)
