# Knowledge Engine

Corpus-agnostic knowledge engine packaged in-tree. Spin-out ready: this `engine/`
subtree is self-contained and can be lifted into its own repo without changes.

## Layout

- `src/knowledge_engine/` — core package
  - `cli.py` — `knowledge-engine` CLI entrypoint; `app.py` / `mcp_server.py` — compat shims
  - `core/` — env config + path resolver (`KE_HOME`, `KE_CORPUS_ROOT`, `KE_DATA_DIR`), corpus registry (source of truth + SQLite mirror), watchdog auto-register
  - `server/` — FastAPI app factory + HTTP route modules (`api/`) + packaged dashboard (`static/`, served at `/ui/`)
  - `mcp/` — MCP server on the official MCP Python SDK (stdio + streamable HTTP)
  - `generation/` — provider abstraction (echo / cloud HTTP / Ollama)
  - `retrieval/` — FTS5 indexer (`fts5.py`) + optional bge-m3 embeddings
  - `project_context/` — project/lane memory: scanner, pointers, git/log context, embeddings, MCP tools
  - `agent_board/` — scoped board store, HTTP/CLI/MCP surfaces, sweeper, provider credential bindings
  - `tool_host/` — hosted script/service/static tool registry
  - `runtime/` — execution substrate: `foundation/` (SQLite backbone), `pipeline/`, `sandbox/` hook
- `routing_local/` — **opt-in** Ollama / local-LLM provider; not imported unless installed
- `sandbox_adapter/` — **opt-in** Docker / WSL sandbox adapter
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
project-context, hosted-tool, and sandbox surfaces. Empty tool tables are reported
as empty data, not as missing features.

## Configuration (env)

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `KE_HOME` | nearest dir (cwd upward) with `corpus/registry.json`, else cwd | Base for the defaults below |
| `KE_CORPUS_ROOT` | `${KE_HOME}/corpus` | Root of the corpus tree |
| `KE_DATA_DIR` | `${KE_HOME}/.knowledge-engine/data` | Runtime SQLite + index location (created on first write) |
| `KE_REGISTRY_PATH` | `${KE_CORPUS_ROOT}/registry.json` | Registry file |

No path is hardcoded.
