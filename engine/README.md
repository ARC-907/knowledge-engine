# Knowledge Engine (package)

The `knowledge-engine` Python package. This `engine/` subtree is
self-contained — its own `pyproject.toml`, lockfile and tests — and is what the
release workflow builds into an sdist and wheel. The repository root holds the
corpus, the docs and the dashboard toolchain around it.

## Layout

- `src/knowledge_engine/`
  - `cli/` — the `knowledge-engine` CLI: parser and dispatch in `__init__.py`
    (`-v`/`-q` set the stderr log level), one module per command group
    (`corpus`, `registry`, `embeddings`, `serve`, `mcp`, `board`,
    `project_context`), imported lazily.
  - `core/` — `paths.py` (`KE_HOME`, `KE_CORPUS_ROOT`, `KE_DATA_DIR`,
    `KE_REGISTRY_PATH` resolver), `settings.py` (typed settings for every
    environment variable), `config.py`, `registry.py` (`registry.json`, the
    source of truth, validated against `registry.schema.json`), `watcher.py`
    (auto-register), `http.py` (CORS, body cap, proxy policy),
    `logging_config.py`, `logsafe.py`, `ollama.py` (one Ollama client), `util/`.
  - `server/` — `app.py` (I/O-free FastAPI factory), `lifespan.py`, `deps.py`,
    `schemas.py`, `api/` route modules, `static/` (the dashboard, served at
    `/ui/`).
  - `mcp/` — MCP server on the official MCP Python SDK (`server.py`), stdio and
    streamable HTTP (`http.py`), legacy tool-group adapter (`toolkit.py`).
  - `generation/` — provider abstraction: echo, Ollama, OpenAI-compatible HTTP.
  - `retrieval/` — `fts5.py` (FTS5 index), `embeddings/` (corpus embedding
    index), `hybrid.py` (`fts` / `vector` / `hybrid` modes).
  - `project_context/` — per-project memory: scanner, ingest, pointers, git
    and log context, embeddings, MCP tool group, CLI.
  - `agent_board/` — scoped message board: store, schemas, scopes, keys,
    provider credential bindings, sweeper, HTTP package, CLI, MCP tool group,
    standalone service.
  - `tool_host/` — hosted script / service / static tool registry.
  - `runtime/` — `foundation/` (YAML config + versioned SQLite backbone),
    `pipeline/` (queue, message board, worker registry, task classifier),
    `sandbox/` (execution adapter hook).
  - `app.py`, `mcp_server.py` — compatibility shims (`mcp_server` is
    deprecated).
- `tests/` — pytest suite; `tests/snapshots/` pins the CLI help, the OpenAPI
  document and the MCP tool list.

## Run

From the repository root:

```bash
uv sync --project engine                 # all dependency groups (dev tools included)
uv run --project engine knowledge-engine --help
uv run --project engine knowledge-engine serve
```

Or run the ASGI app under uvicorn yourself:

```bash
KE_HOME=/path/to/checkout uv run --project engine \
  uvicorn knowledge_engine.app:create_app --factory --host 127.0.0.1 --port 9210
```

(`knowledge-engine serve` does the same and additionally wires logging and the
proxy-header policy; prefer it unless you need uvicorn flags.)

Capability inventory:

```bash
uv run --project engine knowledge-engine capabilities
```

This prints registry counts plus the base MCP tools and the project-context,
agent-board, hosted-tool and sandbox surfaces. Empty tool tables are reported
as empty data, not as missing features.

## Configuration

Every variable is declared on `knowledge_engine.core.settings.Settings` and
documented in [`01-user-docs/CONFIGURATION.md`](../01-user-docs/CONFIGURATION.md)
(generated from that module). The paths:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `KE_HOME` | nearest directory at or above the cwd with `corpus/registry.json`, else the cwd | Base for the defaults below |
| `KE_CORPUS_ROOT` | `<KE_HOME>/corpus` | Root of the corpus tree |
| `KE_REGISTRY_PATH` | `<KE_CORPUS_ROOT>/registry.json` | Registry file |
| `KE_DATA_DIR` | `<KE_HOME>/.knowledge-engine/data` | Runtime SQLite databases and indexes (created on first write) |

`knowledge-engine config show` prints the effective values with secrets masked.

## Development

```bash
uv run --project engine --directory engine ruff format --check .
uv run --project engine --directory engine ruff check .
uv run --project engine --directory engine mypy
uv run --project engine --directory engine pytest -q
```

or `just check` from the repository root. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md).
