"""Embedding index for project-docs.

Builds and queries vector embeddings over the project-docs content store. Each
record's searchable body (stored in ``project_doc_bodies``) is embedded by an
embedding provider (see :mod:`embeddings.providers`), packed to a float32 blob,
and persisted in the ``doc_embeddings`` table keyed by
``(record_id, provider, model)``.

Every function here is offline-capable with the deterministic
:class:`embeddings.providers.StubProvider`. No network I/O is performed in this
module; the provider abstraction owns any remote behavior and is config-gated
upstream.

Functions:

- :func:`generate` -- embed the searchable bodies of project_docs rows.
- :func:`refresh` -- re-embed changed (or all) rows.
- :func:`semantic_search` -- rank stored vectors by cosine to a query vector.
- :func:`similar_records` -- rank stored vectors by cosine to one record.
- :func:`cluster_records` -- simple greedy grouping of stored vectors.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import providers

__all__ = [
    "generate",
    "refresh",
    "semantic_search",
    "similar_records",
    "cluster_records",
]


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _provider_model(provider: providers.EmbeddingProvider) -> str:
    """Return a stable model identifier for ``provider``.

    The ``model`` column is part of the embedding primary key. Providers that
    expose a ``model`` attribute use it; otherwise the provider name doubles as
    the model so the row is still uniquely keyed.
    """
    model = getattr(provider, "model", None)
    if model:
        return str(model)
    return provider.name


def _fetch_docs(
    conn: sqlite3.Connection,
    record_ids: list[str] | None,
) -> list[tuple[str, str]]:
    """Return ``(record_id, searchable_body)`` pairs for the requested rows.

    Bodies live in ``project_doc_bodies``; a record without a body row yields an
    empty body. Filters to ``record_ids`` when provided, else returns every row.
    """
    base = (
        "SELECT pd.record_id AS record_id, "
        "COALESCE(b.searchable_body, '') AS searchable_body "
        "FROM project_docs pd "
        "LEFT JOIN project_doc_bodies b ON b.record_id = pd.record_id"
    )
    if record_ids is None:
        cur = conn.execute(base)
        return [
            (str(r["record_id"]), str(r["searchable_body"]))
            for r in cur.fetchall()
        ]

    pairs: list[tuple[str, str]] = []
    for record_id in record_ids:
        cur = conn.execute(base + " WHERE pd.record_id = ?", (record_id,))
        row = cur.fetchone()
        if row is not None:
            pairs.append((str(row["record_id"]), str(row["searchable_body"])))
    return pairs


def _store_embedding(
    conn: sqlite3.Connection,
    record_id: str,
    provider: providers.EmbeddingProvider,
    vector: list[float],
    created_at: str,
) -> None:
    """Upsert one packed embedding row for ``record_id``."""
    blob = providers.pack_vector(vector)
    conn.execute(
        """
        INSERT INTO doc_embeddings
            (record_id, provider, model, dim, vector, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(record_id, provider, model) DO UPDATE SET
            dim = excluded.dim,
            vector = excluded.vector,
            created_at = excluded.created_at
        """,
        (
            record_id,
            provider.name,
            _provider_model(provider),
            len(vector),
            blob,
            created_at,
        ),
    )


def generate(
    project_conn: sqlite3.Connection,
    provider: providers.EmbeddingProvider,
    *,
    record_ids: list[str] | None = None,
    model: str | None = None,
) -> int:
    """Embed searchable bodies of project_docs rows and store the vectors.

    Embeds the rows identified by ``record_ids`` (or every row when ``None``)
    using ``provider`` and writes one packed vector per row into
    ``doc_embeddings`` (provider/model/dim columns plus a UTC ``created_at``).
    The ``model`` argument is accepted for signature compatibility; the stored
    model is derived from the provider, so callers select a model by passing a
    matching provider. Returns the number of vectors written.
    """
    docs = _fetch_docs(project_conn, record_ids)
    if not docs:
        return 0

    texts = [body for _, body in docs]
    vectors = provider.embed(texts)
    created_at = _utc_now()

    count = 0
    for (record_id, _body), vector in zip(docs, vectors):
        _store_embedding(project_conn, record_id, provider, vector, created_at)
        count += 1
    project_conn.commit()
    return count


def refresh(
    project_conn: sqlite3.Connection,
    provider: providers.EmbeddingProvider,
    **kwargs: object,
) -> int:
    """Re-embed changed (or all) records.

    Accepts the same keyword arguments as :func:`generate`
    (``record_ids``, ``model``). When ``record_ids`` is omitted, every row is
    re-embedded. Because :func:`generate` upserts on
    ``(record_id, provider, model)``, :func:`refresh` simply overwrites any
    existing vectors with freshly computed ones. Returns the number of vectors
    written.
    """
    record_ids = kwargs.get("record_ids")
    model = kwargs.get("model")
    return generate(
        project_conn,
        provider,
        record_ids=record_ids,  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
    )


def _load_vectors(
    conn: sqlite3.Connection,
    provider: providers.EmbeddingProvider,
) -> list[tuple[str, list[float]]]:
    """Return ``(record_id, vector)`` pairs stored for ``provider``/model."""
    cur = conn.execute(
        """
        SELECT record_id, vector
        FROM doc_embeddings
        WHERE provider = ? AND model = ?
        """,
        (provider.name, _provider_model(provider)),
    )
    return [
        (str(row["record_id"]), providers.unpack_vector(row["vector"]))
        for row in cur.fetchall()
    ]


def _summaries_for(
    conn: sqlite3.Connection,
    record_ids: list[str],
) -> dict[str, str]:
    """Return a ``record_id -> summary`` map for ``record_ids``."""
    if not record_ids:
        return {}
    placeholders = ",".join("?" for _ in record_ids)
    cur = conn.execute(
        "SELECT record_id, summary FROM project_docs "
        f"WHERE record_id IN ({placeholders})",
        record_ids,
    )
    return {
        str(row["record_id"]): str(row["summary"] or "")
        for row in cur.fetchall()
    }


def semantic_search(
    project_conn: sqlite3.Connection,
    provider: providers.EmbeddingProvider,
    query: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """Rank stored vectors by cosine similarity to ``query``.

    Embeds ``query`` with ``provider`` and compares it against every vector
    stored for that provider/model. Returns up to ``limit`` results, each a
    dict with ``record_id``, ``score`` (cosine similarity) and ``summary``,
    ordered by descending score. Returns an empty list when no vectors exist.
    """
    pairs = _load_vectors(project_conn, provider)
    if not pairs:
        return []

    query_vec = provider.embed([query])[0]
    scored = [
        (record_id, providers.cosine(query_vec, vector))
        for record_id, vector in pairs
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    top = scored[: max(0, limit)]

    summaries = _summaries_for(project_conn, [rid for rid, _ in top])
    return [
        {"record_id": rid, "score": score, "summary": summaries.get(rid, "")}
        for rid, score in top
    ]


def similar_records(
    project_conn: sqlite3.Connection,
    provider: providers.EmbeddingProvider,
    record_id: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """Rank stored vectors by cosine similarity to one record's vector.

    Looks up the stored vector for ``record_id`` (for ``provider``/model) and
    returns up to ``limit`` other records ordered by descending cosine
    similarity. The query record itself is excluded. Returns an empty list when
    the record has no stored vector.
    """
    pairs = _load_vectors(project_conn, provider)
    target: list[float] | None = None
    for rid, vector in pairs:
        if rid == record_id:
            target = vector
            break
    if target is None:
        return []

    scored = [
        (rid, providers.cosine(target, vector))
        for rid, vector in pairs
        if rid != record_id
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    top = scored[: max(0, limit)]

    summaries = _summaries_for(project_conn, [rid for rid, _ in top])
    return [
        {"record_id": rid, "score": score, "summary": summaries.get(rid, "")}
        for rid, score in top
    ]


def cluster_records(
    project_conn: sqlite3.Connection,
    *,
    k: int = 5,
) -> list[dict]:
    """Group stored vectors into at most ``k`` clusters (basic).

    Performs a simple, deterministic greedy clustering: each record joins the
    existing cluster whose seed is most similar (above a fixed threshold), or
    starts a new cluster while fewer than ``k`` clusters exist, otherwise it
    falls back to the nearest seed. Vectors of every stored provider/model are
    pooled. Returns a list of cluster dicts, each with a ``cluster`` index and a
    ``record_ids`` list. Returns an empty list when no vectors are stored.
    """
    cur = project_conn.execute(
        "SELECT record_id, vector FROM doc_embeddings ORDER BY record_id"
    )
    rows = cur.fetchall()
    if not rows or k <= 0:
        return []

    seeds: list[list[float]] = []
    clusters: list[list[str]] = []
    threshold = 0.5

    for row in rows:
        record_id = str(row["record_id"])
        vector = providers.unpack_vector(row["vector"])

        best_idx = -1
        best_score = threshold
        for idx, seed in enumerate(seeds):
            score = providers.cosine(vector, seed)
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx >= 0:
            clusters[best_idx].append(record_id)
        elif len(clusters) < k:
            seeds.append(vector)
            clusters.append([record_id])
        else:
            nearest = max(
                range(len(seeds)),
                key=lambda i: providers.cosine(vector, seeds[i]),
            )
            clusters[nearest].append(record_id)

    return [
        {"cluster": idx, "record_ids": members}
        for idx, members in enumerate(clusters)
    ]
