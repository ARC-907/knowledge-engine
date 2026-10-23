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

- `cli.py` — `knowledge-engine` console-script entrypoint
  (`info | bootstrap | capabilities | reindex | watch | search | serve | mcp | project-context | board | board-serve`)
- `app.py`, `mcp_server.py` — compatibility shims re-exporting `server.app` / `mcp.server` (`mcp_server` is deprecated)
- `core/` — `settings.py` (typed `Settings`: every `KE_*` / `OLLAMA_*` env var; `config show`, generates `01-user-docs/CONFIGURATION.md`), `config.py` (path view over `Settings`), `errors.py` (shared HTTP exception handlers), `registry.py` (JSON registry + SQLite mirror), `watcher.py` (`watchdog` auto-register)
- `server/` — `app.py` (I/O-free FastAPI application factory), `lifespan.py` (opens/closes the registry, index, providers, board sweeper and MCP mount), `deps.py` (`Depends` providers), `schemas.py` (request/response models), and `api/` route modules: `health_routes`, `registry_routes`, `search_routes`, `generate_routes`, `board_routes` (alias of `agent_board/http.py`)
- `mcp/server.py` — MCP server on the official MCP Python SDK, stdio + streamable HTTP (protocol `2026-07-28`; `initialize` clients back to `2024-11-05`); `mcp/toolkit.py` mounts the legacy JSON-schema tool groups
- `generation/` — provider abstraction: `base.py` (Provider contract + `ProviderRegistry`), `EchoProvider` + `CloudHTTPProvider` (OpenAI-compatible, `cloud_http.py`), `OllamaProvider` (`ollama_provider.py`), `factory.py` (registry from `Settings`)
- `retrieval/` — `fts5.py` (SQLite FTS5 indexer with `bm25` ranking + snippet highlighting) + opt-in bge-m3 embeddings (`embeddings/build.py` + `search.py`)
- `project_context/` — opt-in project/lane documentation subsystem: scanner, ingest, embeddings, git context, pointers, registry; `mcp_tools/` group + `knowledge-engine project-context` CLI
- `agent_board/` — first-class coordination surface: `schemas.py`, `scopes.py`, `store.py`, `keys.py`, `provider_registry.py`, `sweeper.py`, `service.py`, `cli.py`, `mcp_tools/` (auto-discovered MCP tool group)
- `tool_host/` — opt-in tool host: `host.py` (HTTP-addressable script/service/static tool registry)
- `runtime/` — execution substrate: `foundation/` (`config.py` YAML loader + `db.py` SQLite WAL backbone, schema auto-create, FTS5 board mirror), `pipeline/` (`queue.py`, `message_board.py`, `worker_registry.py`, `task_classifier.py`), `sandbox/` (`NoopSandbox` + `get_sandbox()` hook)

### Optional adjuncts

- `engine/routing_local/ollama_provider.py` — opt-in Ollama provider module
- `engine/sandbox_adapter/` — opt-in sandbox scaffold (README only — bring your own executor)

### Tests (`engine/tests/`)

- `test_smoke.py` — registry CRUD roundtrip; indexer rebuild + search; FastAPI factory + `/health` + `/info`; search/reindex/registry routes under the worker threadpool (SQLite cross-thread regression guard); capability inventory shape.
- `test_agent_board.py` — board schema validation + per-field caps, store roundtrip, FTS5 search, digest summarization, atomic ack, sweeper lease/force/threshold-clamp, key vault CRUD + master uniqueness, config, HTTP route smoke, peer-trust gate, `prune_by_count` preservation, and MCP tool discovery + dispatch.
- `test_host_allowlist.py` — hosted-tool host allowlist enforcement.
- `project_context/` — the project-context subsystem test suite (config, db, ingest, scanner, embeddings, pointers, git context, query/registry/log MCP tools, etc.), with fixtures under `tests/fixtures/sample_project/`.

## Dashboard

- `engine/src/knowledge_engine/server/static/` — Alpine.js dashboard auto-mounted at `/ui/` (Search / Registry / Board / Config tabs): `index.html` (markup), `app.js` (component), `tailwind.css` (compiled, committed), `vendor/alpinejs/` (pinned)
- `ui-src/` — Tailwind v4 build toolchain for `tailwind.css` (`npm run build:css`; dev-time only, see `01-user-docs/THEMING.md`)

## Scripts (`scripts/`)

- `vendor.py` — download + SHA-384-verify the vendored dashboard assets (Alpine.js); `--check` verifies the committed files offline
- `mcp-client-config.example.json` — MCP client wiring template
- `agent-board/` — optional standalone Agent Board service: `board-watchdog.ps1` (Windows watchdog; restarts only its own child), `README.md`

## Documentation

- `README.md` — overview + quickstart (repo root)
- `01-user-docs/` — user guides: `README` (index), `QUICKSTART`, `BRING-YOUR-OWN-CORPUS`, `LIBRARY-AUTHORING`, `SKILL-AUTHORING`, `MCP-WIRING`, `EMBEDDINGS`, `DEPLOYMENT`, `THEMING`, `FAQ`
- `02-dev-docs/` — engine internals: `ARCHITECTURE`, `CATALOG` (this file), `CHANGELOG`, `DEPENDENCIES`, `RESEARCH-LOG`, `AGENT-BOARD`, `GLOSSARY`
- `LICENSE` — MIT (covers the whole project)
