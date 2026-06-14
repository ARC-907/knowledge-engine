"""Tests for the embeddings index (offline, StubProvider only).

A fresh project DB (migrations 002-007) is created per test. Records are
inserted directly into ``project_docs`` and ``project_doc_bodies`` so the index
layer can be exercised without the full ingestion pipeline. No network access is
made: the deterministic :class:`StubProvider` produces all vectors.
"""
from __future__ import annotations

import sqlite3

import pytest

from knowledge_engine.project_docs.db import apply_migrations, connect
from knowledge_engine.project_docs.embeddings import index, providers

_PROJECT_PREFIXES = ("002_", "003_", "004_", "005_", "006_", "007_")


@pytest.fixture()
def project_conn(tmp_path) -> sqlite3.Connection:
    """A project DB connection with the project-side migrations applied."""
    conn = connect(tmp_path / "proj.sqlite")
    apply_migrations(conn, only_prefixes=_PROJECT_PREFIXES)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def provider() -> providers.StubProvider:
    """A deterministic stub provider."""
    return providers.StubProvider()


def _insert_doc(
    conn: sqlite3.Connection,
    record_id: str,
    searchable_body: str,
    summary: str = "",
) -> None:
    """Insert one record across project_docs + project_doc_bodies."""
    conn.execute(
        "INSERT INTO project_docs "
        "(record_id, project_fp, branch_fp, category, content_hash, "
        " created_at, updated_at, summary) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            record_id,
            "proj_test00000000",
            "br_main0000000000",
            "design_note",
            "deadbeef",
            "2026-05-30T00:00:00Z",
            "2026-05-30T00:00:00Z",
            summary,
        ),
    )
    conn.execute(
        "INSERT INTO project_doc_bodies (record_id, searchable_body) VALUES (?,?)",
        (record_id, searchable_body),
    )
    conn.commit()


def test_generate_stores_n_vectors(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """generate embeds every row and stores one vector each."""
    _insert_doc(project_conn, "a", "alpha body about cats")
    _insert_doc(project_conn, "b", "beta body about dogs")
    _insert_doc(project_conn, "c", "gamma body about birds")

    written = index.generate(project_conn, provider)
    assert written == 3

    count = project_conn.execute(
        "SELECT COUNT(*) FROM doc_embeddings"
    ).fetchone()[0]
    assert count == 3

    row = project_conn.execute(
        "SELECT provider, model, dim FROM doc_embeddings WHERE record_id = 'a'"
    ).fetchone()
    assert row["provider"] == provider.name
    # StubProvider has no `model` attribute, so the index uses the name.
    assert row["model"] == provider.name
    assert row["dim"] == provider.dim


def test_generate_subset(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """generate honors an explicit record_ids subset."""
    _insert_doc(project_conn, "a", "alpha")
    _insert_doc(project_conn, "b", "beta")

    written = index.generate(project_conn, provider, record_ids=["a"])
    assert written == 1

    ids = [
        r["record_id"]
        for r in project_conn.execute(
            "SELECT record_id FROM doc_embeddings"
        ).fetchall()
    ]
    assert ids == ["a"]


def test_generate_empty(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """generate returns 0 when there are no docs."""
    assert index.generate(project_conn, provider) == 0


def test_refresh_upserts(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """refresh re-embeds without creating duplicate rows."""
    _insert_doc(project_conn, "a", "alpha")
    index.generate(project_conn, provider)
    written = index.refresh(project_conn, provider)
    assert written == 1

    count = project_conn.execute(
        "SELECT COUNT(*) FROM doc_embeddings"
    ).fetchone()[0]
    assert count == 1


def test_semantic_search_returns_match_first(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """The exact-match record ranks first in semantic search."""
    _insert_doc(project_conn, "a", "the quick brown fox", summary="fox")
    _insert_doc(project_conn, "b", "lorem ipsum dolor sit", summary="latin")
    _insert_doc(project_conn, "c", "completely unrelated text", summary="other")

    index.generate(project_conn, provider)

    results = index.semantic_search(
        project_conn, provider, "the quick brown fox", limit=3
    )
    assert results
    assert results[0]["record_id"] == "a"
    assert results[0]["summary"] == "fox"
    # Exact match cosine is ~1.0 for the deterministic stub.
    assert results[0]["score"] == pytest.approx(1.0, abs=1e-6)
    # Scores are sorted descending.
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_semantic_search_empty(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """semantic_search returns [] when nothing is embedded."""
    assert index.semantic_search(project_conn, provider, "anything") == []


def test_semantic_search_limit(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """semantic_search respects the limit argument."""
    for i in range(5):
        _insert_doc(project_conn, f"r{i}", f"body number {i}")
    index.generate(project_conn, provider)

    results = index.semantic_search(
        project_conn, provider, "body number 0", limit=2
    )
    assert len(results) == 2


def test_similar_records_returns_neighbors(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """similar_records returns other records, excluding the query."""
    _insert_doc(project_conn, "a", "alpha body")
    _insert_doc(project_conn, "b", "beta body")
    _insert_doc(project_conn, "c", "gamma body")
    index.generate(project_conn, provider)

    results = index.similar_records(project_conn, provider, "a", limit=10)
    ids = {r["record_id"] for r in results}
    assert "a" not in ids
    assert ids == {"b", "c"}
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_similar_records_missing(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """similar_records returns [] when the record has no vector."""
    _insert_doc(project_conn, "a", "alpha")
    index.generate(project_conn, provider)
    assert index.similar_records(project_conn, provider, "nope") == []


def test_cluster_records_basic(
    project_conn: sqlite3.Connection,
    provider: providers.StubProvider,
) -> None:
    """cluster_records groups all stored records into <= k clusters."""
    for i in range(6):
        _insert_doc(project_conn, f"r{i}", f"body {i}")
    index.generate(project_conn, provider)

    clusters = index.cluster_records(project_conn, k=3)
    assert clusters
    assert len(clusters) <= 3
    members = sorted(rid for c in clusters for rid in c["record_ids"])
    assert members == [f"r{i}" for i in range(6)]


def test_cluster_records_empty(
    project_conn: sqlite3.Connection,
) -> None:
    """cluster_records returns [] when nothing is embedded."""
    assert index.cluster_records(project_conn, k=3) == []
