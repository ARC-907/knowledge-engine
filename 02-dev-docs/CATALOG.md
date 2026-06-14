---
title: Knowledge-Engine — Catalog
status: v1.2
created: 2026-05-11
last_updated: 2026-06-14
---

# Catalog

> Inventory of what actually ships in the repo. The engine is corpus-agnostic:
> it ships a small demo corpus and you add your own content under `corpus/`.

## Corpus (`corpus/`)

- `registry.json` — source-of-truth list of enabled libraries / skills / kits / tools
- `registry.schema.json` — JSON Schema the registry is validated against
- `samples/` — seed content used by the first-run demo and tests (pure synthetic, zero PII):
  - `demo-library/` — minimal library (4 files): `README.md`, `CATALOG.md`,
    `00-foundations/overview.md`, `01-applied/example.md`. Declares the
    three-lens framework **CONCEPT → PATTERN → APPLY**.
  - `demo-skill/` — minimal skill (2 files): `SKILL.md`, `procedure.md`
  - `README.md` — explains the samples folder
  - `assets/.gitkeep` — placeholder for a binary-asset folder

Both demo entries are enabled in `registry.json`, so `knowledge-engine reindex`
has real content to index on a fresh clone. The registry recognizes four entry
kinds — `library`, `skill`, `kit`, `tool` — but only the two demo entries ship;
add your own under `corpus/` and register them (see
[`BRING-YOUR-OWN-CORPUS.md`](../01-user-docs/BRING-YOUR-OWN-CORPUS.md)).

## Engine modules (`engine/src/knowledge_engine/`)

- `app.py` — FastAPI application factory
- `cli.py` — `knowledge-engine` console script
  (`info | bootstrap | capabilities | reindex | watch | search | serve | mcp | project-docs | board | board-serve`)
- `config.py` — env-driven path/URL config (`KE_*`, `OLLAMA_BASE_URL`)
- `indexer.py` — SQLite FTS5 indexer with `bm25` ranking + snippet highlighting
- `mcp_server.py` — JSON-RPC 2.0 MCP stdio server (protocol `2024-11-05`)
- `registry.py` — JSON-backed registry + SQLite mirror
- `watcher.py` — `watchdog`-based auto-register of new corpus folders
- `api/` — FastAPI route modules: `health_routes`, `registry_routes`, `search_routes`, `generate_routes`, `board_routes`
- `embeddings/` — opt-in bge-m3 embedding index + cosine search (`build.py` + `search.py`)
- `routing/` — provider abstraction: `EchoProvider`, `CloudHTTPProvider`, `OllamaProvider`
- `sandbox/` — `NoopSandbox` scaffold + `get_sandbox()` factory hook
- `foundation/` — opt-in pipeline foundation: `config.py` (YAML loader) + `db.py` (SQLite WAL backbone, schema auto-create, FTS5 board mirror)
- `pipeline/` — opt-in multi-worker pipeline: `queue.py`, `message_board.py`, `worker_registry.py`, `task_classifier.py`
- `agent_board/` — first-class coordination surface: `schemas.py`, `scopes.py`, `store.py`, `keys.py`, `providers.py`, `sweeper.py`, `service.py`, `cli.py`, `mcp_tools/` (auto-discovered MCP tool group)
- `project_docs/` — opt-in project/lane documentation subsystem: scanner, ingest, embeddings, git context, pointers, registry; `mcp_tools/` group + `knowledge-engine project-docs` CLI
- `tools/` — opt-in tool host: `host.py` (HTTP-addressable script/service/static tool registry)

### Optional adjuncts

- `engine/routing_local/ollama_provider.py` — opt-in Ollama provider module
- `engine/sandbox_adapter/` — opt-in sandbox scaffold (README only — bring your own executor)

### Tests (`engine/tests/`)

- `test_smoke.py` — registry CRUD roundtrip; indexer rebuild + search; FastAPI factory + `/health` + `/info`; search/reindex/registry routes under the worker threadpool (SQLite cross-thread regression guard); capability inventory shape.
- `test_agent_board.py` — board schema validation + per-field caps, store roundtrip, FTS5 search, digest summarization, atomic ack, sweeper lease/force/threshold-clamp, key vault CRUD + master uniqueness, config, HTTP route smoke, peer-trust gate, `prune_by_count` preservation, and MCP tool discovery + dispatch.
- `test_host_allowlist.py` — hosted-tool host allowlist enforcement.
- `project_docs/` — the project-docs subsystem test suite (config, db, ingest, scanner, embeddings, pointers, git context, query/registry/log MCP tools, etc.), with fixtures under `tests/fixtures/sample_project/`.

## Dashboard

- `ui/index.html` — single-file Alpine.js + Tailwind dashboard auto-mounted at `/ui/` (Search / Registry / Board / Config tabs)

## Scripts (`scripts/`)

- `install.ps1` / `install.sh` — venv + editable install
- `serve.ps1` / `serve.sh` — `KE_*` env + uvicorn launch on port 9210
- `mcp-client-config.example.json` — MCP client wiring template
- `update-vendor.ps1` / `update-vendor.sh` — refresh the vendored dashboard JS (Alpine.js, Tailwind)
- `agent-board/` — optional standalone Agent Board service: `start-board.bat` (Windows launcher), `board-watchdog.ps1` (Windows watchdog), `serve-board.sh` (POSIX foreground launcher), `README.md`

## Documentation

- `README.md` — overview + quickstart (repo root)
- `01-user-docs/` — user guides: `README` (index), `QUICKSTART`, `BRING-YOUR-OWN-CORPUS`, `LIBRARY-AUTHORING`, `SKILL-AUTHORING`, `MCP-WIRING`, `EMBEDDINGS`, `DEPLOYMENT`, `THEMING`, `FAQ`
- `02-dev-docs/` — engine internals: `ARCHITECTURE`, `CATALOG` (this file), `CHANGELOG`, `DEPENDENCIES`, `RESEARCH-LOG`, `AGENT-BOARD`, `GLOSSARY`
- `LICENSE` — MIT (covers the whole project)
