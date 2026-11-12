# Embeddings — Semantic and Hybrid Search

Knowledge-Engine's default search is **FTS5**: fast, lexical, keyword-based. It
matches the words you type. For most corpora that is enough.

The engine also ships an **opt-in embedding index** that matches by *meaning*.
Ask for "how to make decisions under pressure" and it can surface a file about
the OODA loop even if those exact words never appear. **Hybrid** search merges
both rankings.

## Search modes

Every search surface takes a `mode`:

| Mode | What it does | Needs |
| --- | --- | --- |
| `fts` (default) | FTS5 keyword search, BM25 ranking, `<mark>` snippets | `knowledge-engine reindex` |
| `vector` | Files ranked by the cosine similarity of their best-matching chunk | the embedding index |
| `hybrid` | Both rankings merged with reciprocal rank fusion (k = 60) | both indexes |

```pwsh
knowledge-engine search "decisions under pressure" --mode hybrid
```

- **HTTP:** `GET /search?q=...&mode=hybrid`. The response gains `mode`; hits
  gain `fused_score` (hybrid), `vector_score` (cosine) and `fts_score` (BM25,
  hybrid hits FTS also found). `score` is "lower is better" in every mode.
- **MCP:** the `search` tool takes an optional `mode` (default `fts`).

`vector` and `hybrid` only return files of **enabled** registry entries (and
honour `kind`), like FTS. Leaving `mode` out keeps the FTS behaviour unchanged.

## What you need

Embeddings come from an **Ollama** embedding model running locally (default):

1. **Ollama installed and running.** Get it from <https://ollama.com>.
2. **An embedding model pulled.** The default is `bge-m3`:
   ```
   ollama pull bge-m3
   ```
   Any Ollama embedding model works — set `KE_EMBED_MODEL` to use another.

No cloud API, no keys. Two other providers exist: `remote` (an HTTP endpoint;
sends your corpus text off-machine, so it needs `KE_EMBED_ALLOW_REMOTE=1`) and
`fake` (offline, deterministic vectors for tests and dry runs — not semantic).

## Building the index

```pwsh
# full build (re-embeds every file, drops files that left the corpus)
knowledge-engine embeddings build

# incremental — only re-embed files that changed
knowledge-engine embeddings build --incremental

# what the index holds, and which model built it
knowledge-engine embeddings status

# progress on stderr
knowledge-engine -v embeddings build
```

The builder walks every markdown file under the corpus root (skipping
dot-directories, `node_modules` and similar, and files under 50 characters),
splits each into overlapping chunks, embeds them in batches and stores chunks
plus vectors in SQLite.

Chunking: about **1500 characters** per chunk with **200 characters** of
overlap, cut at line boundaries, each labelled with its nearest heading. Only
the first **2000 characters** of a chunk are sent to the model (a chunk can be
longer when a single line is very long); the full chunk is stored. Change them
with `--chunk-size`, `--chunk-overlap` and `--max-chars`; a changed setting
re-embeds every file.

Other `build` options: `--provider fake|ollama|remote`, `--model`, `--batch`
(chunks per request), `--db` (index file), `--rebuild` (drop the index first),
`--backend scan|sqlite-vec` (see [backends](#vector-search-backends)).

### One model per index

The index records the provider, **model**, vector **dimension**, chunking
and timestamps. It never mixes models: building or searching with a different
model fails with an `embedding_model_mismatch` error that names both models.
To switch models, rebuild:

```pwsh
$env:KE_EMBED_MODEL = "nomic-embed-text"
knowledge-engine embeddings build --rebuild
```

(`bge-m3` and `bge-m3:latest` count as the same model.)

## Searching the index directly

```pwsh
# top 10 most similar chunks
knowledge-engine embeddings search "your query"

# fewer hits, one top-level corpus subdirectory
knowledge-engine embeddings search "your query" --top 5 --library libraries
```

Output is JSON: `score` (cosine similarity), `rel_path`, `library`, `heading`,
`text`, `chunk_idx`. With the default `scan` backend, search streams the stored
vectors, so memory use does not grow with the index.

## Vector search backends

How a `vector` or `hybrid` search finds the nearest chunks is pluggable
(`KE_EMBED_BACKEND`, or `--backend` on the `embeddings` commands):

| Backend | How it searches | Needs |
| --- | --- | --- |
| `scan` (default) | Streams every stored vector through a cosine scan | nothing extra |
| `sqlite-vec` | Nearest-neighbour search in a [sqlite-vec](https://github.com/asg017/sqlite-vec) `vec0` table | the `vec` extra |

Both return **the same results**. The `sqlite-vec` backend is an accelerator,
not an approximation: it asks `vec0` for more chunks than it needs, re-scores
them exactly, and asks for more until no chunk it has not seen can beat the
ones it returns. Filters (enabled entries, `kind`) still apply before the
result is cut to `limit`. If one query would need more than 4096 chunks
(a very selective filter), that query is answered by the scan and a WARNING
is logged. Exact ties are broken by the lower chunk id in both backends.

Rough timings on one machine, 20,000 random 1024-dimensional vectors:

| Query | `scan` | `sqlite-vec` |
| --- | --- | --- |
| `embeddings search --top 10` | ~1.4 s | ~60 ms |
| `vector` search, 30 files | ~1.4 s | ~65 ms |
| `hybrid` candidates, 200 files | ~1.4 s | ~150 ms |

For a few thousand chunks the scan is fast enough; the accelerator pays off
on large corpora.

### Installing and enabling sqlite-vec

```pwsh
# from a checkout (in engine/)
uv sync --extra vec
# or from a package index
pip install "knowledge-engine[vec]"

# build (or refresh) the vec table, then use it
knowledge-engine embeddings build --incremental --backend sqlite-vec
$env:KE_EMBED_BACKEND = "sqlite-vec"
knowledge-engine embeddings status
```

The default install does not include the `vec` extra; nothing changes until you
opt in.

The `vec0` table lives in a **sibling database** next to the index
(`embeddings.db` → `embeddings.vec.db`), never inside it, so the index stays
usable and rebuildable without the extension. Your Python's `sqlite3` module
must be able to load extensions (the Python builds `uv` installs can; some
system Pythons cannot).

### Keeping the vec table in sync

A build run with the `sqlite-vec` backend keeps the table in step: the first
one copies every vector; later ones (`--incremental` or full) apply only the
chunks that changed, using a change log the index keeps once the backend is
set up. `--rebuild` with the backend recreates the table. A build run with the
`scan` backend leaves the table behind; it is then **stale** until the next
`sqlite-vec` build.

### Fallback

The accelerator never fails a search. If the package is not installed, the
extension cannot be loaded, the vec table is missing, or it is stale (built
for another model or dimension, behind the index, or the index was rebuilt
without it), searches use the `scan` backend instead:

- the reason is logged once at WARNING;
- `embeddings status` shows `"backend": {"configured": "sqlite-vec", "active": "scan", "warning": ...}`
  and repeats the reason under `warnings`;
- `embeddings build --backend sqlite-vec` reports it under `backend.warning`
  when the table could not be updated (the build itself still succeeds).

The one-model rule is unchanged: a model mismatch is still an
`embedding_model_mismatch` error with either backend.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `KE_EMBED_PROVIDER` | `ollama` | `ollama`, `remote` or `fake` |
| `KE_EMBED_MODEL` | `bge-m3:latest` | The embedding model |
| `KE_EMBED_BATCH` | `32` | Chunks per embedding request |
| `KE_OLLAMA_URL` | `http://127.0.0.1:11434` | Where Ollama is listening |
| `KE_EMBED_REMOTE_URL` | *(empty)* | Endpoint of the `remote` provider |
| `KE_EMBED_ALLOW_REMOTE` | `0` | Opt-in gate of the `remote` provider |
| `KE_EMBEDDINGS_DB` | `$KE_DATA_DIR/embeddings.db` | Where the index is stored |
| `KE_EMBED_BACKEND` | `scan` | `scan` or `sqlite-vec` (see [backends](#vector-search-backends)) |
| `KE_CORPUS_ROOT` | `${KE_HOME}/corpus` | Which corpus to embed |

Command-line flags win over the variables. The full reference is
[CONFIGURATION.md](CONFIGURATION.md).

## How it relates to the rest of the engine

- The **FTS5 index** (`$KE_DATA_DIR/index.db`) and the **embedding index**
  (`$KE_DATA_DIR/embeddings.db`) are separate databases. Building one never
  touches the other.
- The server reads the embedding index per request; an `fts` search never
  touches it, and a server whose embedding provider is misconfigured still
  serves `fts`.
- In code: `knowledge_engine.retrieval.embeddings.CorpusEmbeddingIndex`
  (build/search/status) and `knowledge_engine.retrieval.hybrid.HybridSearcher`
  (the three modes). The older `knowledge_engine.retrieval.embeddings.build` /
  `.search` functions still work on top of them.
- The project-context subsystem has its own embedding store and settings
  (`[project_context.embeddings]`); it shares the provider classes and the
  vector format.

## Keeping the index fresh

The embedding index does not auto-update. After you add or edit corpus content:

```
knowledge-engine embeddings build --incremental
```

Run it on whatever cadence fits — manually after big edits, or on a schedule.
A file whose embedding failed is retried by the next `--incremental` run.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `embedding_index_missing` | Run `knowledge-engine embeddings build` first. An index from an older release must be rebuilt: `--rebuild`. |
| `embedding_model_mismatch` | The index was built with another model. Set `KE_EMBED_MODEL` back to it, or `knowledge-engine embeddings build --rebuild`. |
| `embedding_provider_error` | Ollama isn't running, or isn't at `KE_OLLAMA_URL`. Start it; check the URL. |
| "model not found" | Pull the model: `ollama pull bge-m3` (or whatever `KE_EMBED_MODEL` names). |
| `embedding_unavailable` | `remote` is selected without `KE_EMBED_ALLOW_REMOTE=1` or without `KE_EMBED_REMOTE_URL`. (An unknown `KE_EMBED_PROVIDER` name is not an error: it is logged as a WARNING and `ollama` is used.) |
| Build is slow | Expected on the first full build. Use `--incremental` after; raise `KE_EMBED_BATCH` if your machine copes. |
| Build fails with `timed out after 60s` | Each embedding request must finish within 60 s. A CPU-only Ollama embeds `bge-m3` at roughly 1–2 s per chunk, so lower `KE_EMBED_BATCH` (or `--batch`) to 8–16. |
| `status` shows `"active": "scan"` with a `warning` | The `sqlite-vec` backend is configured but cannot be used; searches still work through the scan. Install the `vec` extra and/or run `knowledge-engine embeddings build --incremental --backend sqlite-vec`. |
| Results feel off | Different embedding models suit different content. Try another model and `--rebuild`. |
