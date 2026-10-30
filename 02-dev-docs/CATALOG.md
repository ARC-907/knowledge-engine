---
title: Knowledge-Engine — Catalog
status: v3.0
created: 2026-05-11
last_updated: 2026-10-23
---

# Catalog

> Inventory of what ships in the repository. The engine is corpus-agnostic: it
> ships a small demo corpus and you add your own content under `corpus/`.

## Corpus (`corpus/`)

- `registry.json` — source-of-truth list of enabled libraries / skills / kits / tools, plus the `lifecycle` block
- `registry.schema.json` — JSON Schema the registry is validated against on every load and write (same rules as the packaged `knowledge_engine/core/registry.schema.json`, with a `$id`, titles and descriptions; `knowledge-engine registry validate` checks a file)
- `samples/` — seed content used by the first-run demo and tests (synthetic, no personal data):
  - `demo-library/` — minimal library (4 files): `README.md`, `CATALOG.md`,
    `00-foundations/overview.md`, `01-applied/example.md`. Declares the
    three-lens framework **CONCEPT → PATTERN → APPLY**.
  - `demo-skill/` — minimal skill (2 files): `SKILL.md`, `procedure.md`
  - `README.md` — explains the samples folder
  - `assets/.gitkeep` — placeholder for a binary-asset folder

Both demo entries are enabled in `registry.json`, so `knowledge-engine reindex`
has real content to index on a fresh clone. The registry recognises four entry
kinds — `library`, `skill`, `kit`, `tool` — but only the two demo entries ship;
add your own under `corpus/` and register them (see
[`BRING-YOUR-OWN-CORPUS.md`](../01-user-docs/BRING-YOUR-OWN-CORPUS.md)).

## Engine package (`engine/`)

- `pyproject.toml` — package metadata (hatchling), the `knowledge-engine` console script, dependency groups, ruff / mypy / pytest / coverage configuration
- `uv.lock` — locked dependency set (`uv lock --check` in CI)
- `README.md` — package layout and run instructions

### Modules (`engine/src/knowledge_engine/`)

- `cli/` — the `knowledge-engine` CLI: `__init__.py` (parser, `-v`/`-q` logging, lazy dispatch, `CommandError`), `__main__.py`, and one module per command group: `corpus.py` (`info`, `bootstrap`, `capabilities`, `reindex`, `watch`, `search`, `config show`), `registry.py` (`registry validate`), `embeddings.py` (`embeddings build|search|status`), `serve.py` (`serve`, `board-serve`), `mcp.py` (`mcp`), `board.py` (`board …`), `project_context.py` (`project-context …`)
- `app.py`, `mcp_server.py` — compatibility shims re-exporting `server.app.create_app` / `mcp.server.main` (`mcp_server` is deprecated and warns on import)
- `core/` — `paths.py` (`KE_HOME` / `KE_*` path resolver), `settings.py` (typed `Settings` for every environment variable; `config show`; generates `01-user-docs/CONFIGURATION.md`), `config.py` (path view over `Settings`), `registry.py` (JSON registry, atomic writes, schema validation), `registry.schema.json`, `watcher.py` (`watchdog` auto-register), `http.py` (CORS helper, body-size cap, proxy policy), `logging_config.py`, `logsafe.py` (log escaping), `errors.py` (HTTP exception handlers), `ollama.py` (`OllamaClient`), `util/` (`env`, `hashing`, `proc`, `slug`, `sqlite` incl. the migration runner and `ConnectionCache`, `time`, `vectors`)
- `server/` — `app.py` (I/O-free FastAPI factory: CORS, body cap, board / MCP / dashboard mounts), `lifespan.py` (opens and closes the registry, index, providers, board sweeper and MCP session manager), `deps.py` (`Depends` providers), `schemas.py` (response models), `api/` (`health_routes`, `registry_routes`, `search_routes`, `generate_routes`, `board_routes` — alias of `agent_board/http/`), `static/` (dashboard: `index.html`, `app.js`, compiled `tailwind.css`, `vendor/alpinejs/`)
- `mcp/` — `server.py` (server on the official MCP Python SDK; `build_server`, `main`, `--selftest`), `http.py` (streamable-HTTP mount, Host-header policy), `options.py` (CLI flags), `tool_groups.py` (optional group discovery), `toolkit.py` (`register_group`: mounts legacy JSON-schema tool groups with argument validation)
- `generation/` — `base.py` (`Provider` contract, `ProviderRegistry`, cached availability probes), `cloud_http.py` (OpenAI-compatible client), `ollama_provider.py`, `factory.py` (registry from `Settings`, `KE_PROVIDER_ORDER`); `EchoProvider` is the always-available fallback
- `retrieval/` — `fts5.py` (SQLite FTS5 indexer with `bm25` ranking, snippets, syntax fallback), `embeddings/` (`index.py` `CorpusEmbeddingIndex`, `factory.py` providers from `Settings`, `search.py`, `build.py`, `errors.py`), `hybrid.py` (`HybridSearcher`: `fts` / `vector` / `hybrid`, reciprocal rank fusion)
- `project_context/` — per-project memory: `config.py` (`knowledge-engine.toml`), `db.py` + `migrations/` (versioned SQL, `registry/` sub-migrations), `registry.py`, `fingerprints.py`, `ingest.py`, `search.py`, `pointers.py`, `git_context.py`, `logs.py`, `sanitize.py`, `scanner/` (discovery, docstrings, comments, markdown, logs, pointer plan / apply, report, validators), `embeddings/` (`LocalOllamaProvider`, remote provider, index), `mcp_tools/` (46 tools), `cli.py`
- `agent_board/` — `schemas.py` (channels, message types, validation), `store.py`, `digest.py`, `scopes.py` (per-scope databases, cap, legacy-name detection), `keys.py` (key vault, `authorize_write` / `authorize_key`), `provider_registry.py` (provider → env-var bindings), `sweeper.py` (`Sweeper`), `http/` (`trust.py`, `limits.py`, `bodies.py`, `messages.py`, `admin.py`; router assembled in `__init__.py`), `service.py` (standalone app), `cli.py` (`knowledge-engine board …`), `mcp_tools/` (15 tools)
- `tool_host/` — `host.py` (script / service / static tool registry with allowlists and proxy hardening)
- `runtime/` — `foundation/` (`config.py` YAML loader + `.env` loading; `db/` versioned SQLite backbone: `connection`, `migrate`, `schema/001–008_*.sql`, `board_config`, `events`, `leases`, `locks`, `rows`), `pipeline/` (`queue.py`, `message_board.py`, `worker_registry.py`, `task_classifier.py`), `sandbox/` (`NoopSandbox`, `get_sandbox()` hook)

### Tests (`engine/tests/`)

- Core and server: `test_smoke.py`, `test_server_app.py`, `test_openapi_snapshot.py`, `test_settings.py`, `test_paths.py`, `test_registry.py`, `test_registry_schema.py`, `test_registry_indexer.py`, `test_search_modes.py`, `test_embeddings_index.py`, `test_fts5_missing_library.py`, `test_generation.py`, `test_ollama_client.py`, `test_core_util.py`, `test_logsafe.py`, `test_versions.py`, `test_import_side_effects.py`, `test_dashboard.py`
- CLI: `test_cli.py` (help snapshot, lazy dispatch, logging flags), `test_cli_serve_port.py`
- MCP: `test_mcp_server.py`, `test_mcp_search.py`
- Agent board: `test_agent_board.py`, `test_board_http.py`, `test_board_security.py`, `test_board_substrate.py`, `test_board_sweeper.py`, `test_board_cli.py`, `test_sweeper_lease.py`
- Substrates: `test_runtime_config.py`, `test_runtime_pipeline.py`, `test_foundation_migrations.py`, `test_sqlite_migrations.py`, `test_connection_lifecycle.py`, `test_tool_host_crud.py`, `test_tool_host_hardening.py`, `test_host_allowlist.py`
- `project_context/` — the project-context suite (config, db, ingest, scanner, embeddings, pointers, git context, MCP tools), with fixtures under `fixtures/sample_project/`
- `ui/` — opt-in Playwright smoke test (`KE_UI_TESTS=1`)
- `snapshots/` — `cli_help.txt`, `openapi.json`, `mcp_tools.json`
- `fixtures/` — `sample_project/`, `registry/` (valid and invalid registries), `pipeline_db/legacy_inline_schema.sql`

## Dashboard

- `engine/src/knowledge_engine/server/static/` — Alpine.js dashboard served at `/ui/` (Search / Registry / Board / Config tabs): `index.html` (markup), `app.js` (component), `tailwind.css` (compiled, committed), `vendor/alpinejs/` (pinned)
- `ui-src/` — Tailwind v4 toolchain for `tailwind.css` (`just ui-css`; dev-time only, see [`THEMING.md`](../01-user-docs/THEMING.md))

## Scripts (`scripts/`)

- `check_docs_paths.py` — verifies that every `knowledge_engine.*` path and relative link in the Markdown docs resolves (`just docs-check`, CI `docs` job)
- `vendor.py` — download + SHA-384-verify the vendored dashboard assets; `--check` verifies the committed files offline
- `mcp-client-config.example.json` — MCP client wiring template
- `agent-board/` — optional standalone Agent Board service: `board-watchdog.ps1` (Windows watchdog; restarts only its own child), `README.md`

## Repository tooling

- `justfile` — task runner recipes (`install`, `lint`, `fmt`, `typecheck`, `test`, `check`, `build`, `docs-check`, `serve`, `mcp`, `ui-css`, `vendor`, …)
- `.pre-commit-config.yaml` — ruff, zizmor, mypy, lockfile check, whitespace hooks
- `.github/workflows/` — `ci.yml` (lint, test matrix, build, zizmor, docs), `release.yml`, `codeql.yml`, `scorecard.yml`; `dependabot.yml`, `CODEOWNERS`, issue forms, PR template
- `Dockerfile`, `compose.yaml`, `.dockerignore` — container image and compose stack

## Documentation

- `README.md` — overview and quickstart (repository root)
- `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `LICENSE` (MIT, covers the whole project)
- `01-user-docs/` — user guides: `README` (index), `QUICKSTART`, `BRING-YOUR-OWN-CORPUS`, `LIBRARY-AUTHORING`, `SKILL-AUTHORING`, `MCP-WIRING`, `EMBEDDINGS`, `DEPLOYMENT`, `CONFIGURATION` (generated), `THEMING`, `FAQ`
- `02-dev-docs/` — engine internals: `ARCHITECTURE`, `CATALOG` (this file), `AGENT-BOARD`, `DEPENDENCIES`, `GLOSSARY`, `RELEASING`, `ROADMAP` (status and plans), `adr/` (decision records), `CHANGELOG` (pointer to the root file)
