# ADR 0017 — Vector search backends: an exact-result seam, sqlite-vec as the first accelerator

- **Status:** Accepted (3.1 candidate "Vector index backend")
- **Date:** 2026-11

## Context

Vector search over the corpus embedding index
(`retrieval.embeddings.index.CorpusEmbeddingIndex`) streams every stored
vector through a Python cosine scan. That is simple and memory-bounded, but
the time grows linearly: about 1.4 s per query for 20,000 1024-dimensional
chunks, paid once per `vector` search and once per `hybrid` search.

Constraints:

- The default install must not change. Users who never opt in get the same
  dependencies and the same results.
- The index already refuses to mix models or dimensions. That rule must hold
  whatever the backend.
- `search_files(accept=...)` filters files *before* the cut (enabled
  entries, `kind`). An accelerator must not turn that into "filter the top
  N", which silently returns fewer hits.
- Search must never fail because of an optional component.

Options considered:

- **FAISS / hnswlib.** Approximate graph indexes: large native wheels and a
  second persistence format next to SQLite. They change results.
- **numpy matrix scan.** A heavy dependency for a constant-factor gain. Still
  linear, and it needs the whole matrix in memory.
- **sqlite-vec.** A small, dependency-free SQLite extension. Wheels exist for
  Linux, macOS (x86-64 and arm64) and Windows. Its `vec0` virtual table does
  exact brute-force KNN in C (float32, cosine), with metadata-column filters,
  inside the storage engine we already use.

## Decision

- **Setting.** `KE_EMBED_BACKEND = scan | sqlite-vec` (default `scan`). The
  `embeddings` CLI commands take `--backend`.
- **Packaging.** sqlite-vec is an optional `vec` extra. The `test`
  dependency group also includes it, so CI exercises the backend on every OS.
- **Seam.** `retrieval.embeddings.backends.VectorAccelerator` is one method:
  `nearest(query, k, library) -> chunk ids, best first`. Any backend can
  return approximate answers. The index never trusts the order: it re-scores
  each candidate exactly from the stored vector, using the same scorer as the
  scan.
- **Exactness proof and refill.** Every chunk the backend did not return is at
  least as far away as the last chunk it did return. So, allowing a margin of
  `1e-3` for float32 rounding, nothing unseen can score above
  `floor = score(last) + margin`.
  - An answer is final once `top` chunks, or `top` accepted files' best
    chunks, score above `floor`, or once the backend has run out of chunks.
  - Until then the request doubles, starting at `max(4 * top, 64)`.
  - At `vec0`'s limit of `k = 4096`, the query is answered by the scan and a
    WARNING is logged.

  Results therefore equal the scan's. Exact ties break by the lower chunk id
  in both backends, which also made the scan's tie-breaking deterministic.
- **Storage.** The `vec0` table lives in a sibling database
  (`embeddings.db` → `embeddings.vec.db`). It holds the vectors and a
  `library` metadata column, with rowid = chunk id. A virtual table whose
  module is not loaded cannot be used or dropped, so keeping it inside the
  index would break `--rebuild` (and older releases) on installations without
  the extension.
- **Sync.**
  - Once the backend is set up, two plain-SQL triggers on `embedding_chunks`
    log every inserted or deleted chunk id into `embedding_vec_log`. Any
    writer fires them, with or without the extension.
  - A build with the backend replays the log: delete each logged id, then
    re-insert it if the chunk exists (ids can be reused). It recreates the
    table when tracking was not installed (first setup, after `--rebuild`), or
    when the recorded model or dimension differs.
  - The mirror records its model, dimension and vector count.
- **Staleness and fallback.** The mirror is not used when:
  - the package or extension cannot be loaded;
  - the table is missing;
  - the model or dimension differs;
  - the triggers are missing;
  - the log is non-empty;
  - its count differs from the index.

  In each case search falls back to the scan. The reason is logged once at
  WARNING and appears in `embeddings status` under `backend.warning` and
  `warnings`. Model mismatches are still `EmbeddingModelMismatchError`, raised
  before any backend is consulted.

## Consequences

- With the backend, a query costs about 60 ms instead of 1.4 s, and building
  `hybrid`'s 200 file candidates about 150 ms (20k × 1024).
- Keeping the mirror costs a full sync of about 4 s for 20k × 1024. After that
  it costs one log row per chunk write, plus replaying the changes at the end
  of each build.
- The default `scan` path is unchanged apart from deterministic tie-breaking.
- The `vec0` search is still linear (brute force in C). A truly sub-linear
  index (sqlite-vec's planned ANN modes, or another library) can be added
  behind the same seam. The re-scoring keeps results exact for as long as
  the backend's recall at the refill sizes is complete. An approximate
  backend that misses candidates would need its own ADR and a documented
  result contract.
- A Python whose `sqlite3` cannot load extensions (some system builds) always
  uses the scan. That is visible in `status`.
