# Knowledge Engine

Corpus-agnostic knowledge engine packaged in-tree. Spin-out ready: this `engine/`
subtree is self-contained and can be lifted into its own repo without changes.

## Layout

- `src/knowledge_engine/` — core package
  - `config.py` — loads paths from env (`KE_CORPUS_ROOT`, `KE_DATA_DIR`)
  - `registry.py` — `corpus/registry.json` is source of truth; SQLite mirror at `data/registry.db`
  - `indexer.py` — FTS5 indexer over enabled libraries / skills / tools
  - `search.py` — query API
  - `app.py` — FastAPI application factory
  - `routing/` — provider abstraction; cloud providers live here
  - `api/` — HTTP route modules
  - `project_docs/` — project/lane memory, scanner, pointers, git/log context, embeddings, MCP tools
  - `agent_board/` — scoped board store, HTTP/CLI/MCP surfaces, sweeper, provider bindings
  - `foundation/` — optional SQLite backbone for hosted tools, pipeline, shared state
  - `tools/` — hosted script/service/static tool registry
  - `sandbox/` — execution-adapter hook
- `routing_local/` — **opt-in** Ollama / local-LLM provider; not imported unless installed
- `sandbox_adapter/` — **opt-in** Docker / WSL sandbox adapter
- `data/` — runtime artifacts (SQLite mirror, indexes); gitignored
- `tests/` — pytest smoke tests

## Run

```bash
pip install -e .[dev]
KE_CORPUS_ROOT=../corpus uvicorn knowledge_engine.app:create_app --factory --reload
```

Capability inventory:

```bash
knowledge-engine capabilities
```

This prints seeded corpus counts plus available retrieval, board,
project-docs, hosted-tool, and sandbox surfaces. Empty tool tables are reported
as empty data, not as missing features.

## Configuration (env)

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `KE_CORPUS_ROOT` | `../corpus` | Root of the corpus tree |
| `KE_DATA_DIR` | `./data` | Runtime SQLite + index location |
| `KE_REGISTRY_PATH` | `${KE_CORPUS_ROOT}/registry.json` | Registry file |

No path is hardcoded.
