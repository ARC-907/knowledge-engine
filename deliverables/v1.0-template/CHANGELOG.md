# Changelog

All notable changes to the Knowledge-Engine.

## [Unreleased]

### Added — Agent Board

A first-class agent coordination surface that promotes the existing
opt-in `pipeline/message_board.py` into a fully-tooled subsystem. Same
SQLite backbone, same port (9210) by default — opt out via
`KE_BOARD_ENABLED=0`.

- **Engine package** (`knowledge_engine.agent_board`) — schema-validated
  channels and message types (`schemas.py`), store facade with FTS5 search
  and context-compressed digest (`store.py`), provider-key vault
  (`keys.py`), background sweeper for TTL prune + stale-blocker reminders
  + per-channel digests (`sweeper.py`), optional standalone FastAPI
  service (`service.py`), and a CLI subcommand (`cli.py`).
- **HTTP API** (`api/board_routes.py`) — 22 routes covering status,
  channels, message types, post/poll, ack, threads, search, digest,
  stats, sweep, key vault, and singleton config. Local-trust by default;
  flip `require_key_for_post` to gate non-localhost writes with
  `X-Board-Key`.
- **MCP tool group** (`agent_board/mcp_tools/`) — 14 auto-discovered
  tools: `board_post`, `board_claim`, `board_release`, `board_blocker`,
  `board_ack`, `board_read`, `board_relevant`, `board_thread`,
  `board_digest` (context-saver), `board_status`, `board_channels`,
  `board_message_types`, `board_search`, `board_sweep_now`.
- **Dashboard** (`ui/index.html`) — tab strip with **Search / Registry /
  Board / Config** tabs. Board tab: channel + type filter, FTS5 search,
  post form, ack button, digest view, manual sweep. Config tab: ports,
  sweeper interval, retention, channels, require-key toggle, provider-
  key vault (create / list / revoke; raw key shown once).
- **Schema additions** (`foundation/db.py`) — `messages_fts` FTS5 virtual
  table mirroring `subject + body` with insert/update/delete triggers,
  `board_sweeps` audit log, `board_config` singleton, plus
  `messages.thread_id` migration column.
- **Standalone watchdog** (`scripts/agent-board/`) — `start-board.bat`,
  `board-watchdog.ps1` (Windows), `serve-board.sh` (POSIX) for headless
  deployments on a separate port (default 11437, mirroring the caprock
  convention so two boards don't collide on the same machine).
- **Buyer guide** (`docs/AGENT-BOARD.md`) — channels, message types,
  HTTP API, MCP tool surface, CLI reference, dashboard walkthrough,
  sweeper details, provider keys, standalone deployment, configuration
  knobs.
- **Tests** (`engine/tests/test_agent_board.py`) — 18 tests covering
  schema validation, store roundtrip, FTS5 search, digest summary, ack,
  sweeper, key vault, config, HTTP route smoke, and MCP tool discovery
  + dispatch.

### Configuration

New env var: `KE_BOARD_ENABLED` (default `1`), `KE_BOARD_SWEEPER`
(default follows board_config), `KE_BOARD_URL` / `KE_BOARD_PORT` /
`KE_BOARD_KEY` for the CLI.

Runtime configuration via `/board/config` (PATCH) or the Config tab —
all changes are persisted in the `board_config` singleton row.

### Verified

- `pytest`: 303 passed, 1 skipped (the optional embedding-build path),
  zero failures across the full suite.

## [1.0.0] — 2026-05-23

### Highlights

The first public release of Knowledge-Engine: a corpus-agnostic, self-hosted
knowledge engine with FTS5 search, an MCP stdio server, a single-file
Alpine.js dashboard, and an optional bge-m3 embedding index. Ships with three
demonstration libraries showing how to organize a knowledge corpus around
domain-tuned three-lens frameworks so that AI agents can navigate it.

### Added

- **Engine package** (`knowledge_engine`) — FastAPI application factory,
  `knowledge-engine` CLI (`bootstrap | reindex | search | info | serve | mcp | watch`),
  registry + indexer, file-watcher, provider routing, JSON-RPC MCP stdio
  server (protocol `2024-11-05`).
- **Dashboard** (`ui/index.html`) — single-file Alpine.js + Tailwind UI auto-mounted
  at `/ui/`; first-run onboarding banner explaining the bring-your-own-corpus
  workflow.
- **MCP server** — four tools exposed to AI assistants:
  `search`, `registry_list`, `registry_toggle`, `registry_get`.
- **Opt-in embedding index** (`knowledge_engine.embeddings`) — bge-m3 / Ollama
  embeddings of every markdown file in the corpus, stored in SQLite for cosine
  search. Independent of the FTS5 indexer.
- **Demonstration corpus** — three reference libraries under `corpus/libraries/`:
  - **Decision Analysis** — three-lens: FRAME → ANALYZE → DECIDE
  - **AI Monetization** — three-lens: IDENTIFY → EVALUATE → EXECUTE
  - **System Design** — three-lens: FRAME → DESIGN → EVOLVE
- **Bundled JSON schemas** (`corpus/schemas/`) — ten schemas for common
  research-data shapes (agent-message, audit-finding, calendar-event,
  cluster-record, conflict-record, handoff-bundle, research-object,
  source-record, task, tool-card).
- **Bundled prompt templates** (`corpus/prompts/`) — twelve agnostic templates:
  three audit, three code, four research, one extraction-worker, one cloud
  synthesis-agent.
- **Buyer documentation** (`docs/`) — index plus QUICKSTART,
  BRING-YOUR-OWN-CORPUS, LIBRARY-AUTHORING, SKILL-AUTHORING, MCP-WIRING,
  EMBEDDINGS, DEPLOYMENT, THEMING, FAQ.
- **Licensing** — MIT for engine + dashboard + demo content (`LICENSE`);
  single-buyer commercial-permissive for the paid Standard bundle
  (`LICENSE-BUYER.md`).
- `corpus/libraries/EMPTY.md` — "how to add a library" reference for every
  empty corpus subdirectory.
- `engine/tests/test_smoke.py` — four pytest smoke tests covering registry
  CRUD, indexer rebuild + search, FastAPI factory, and search/reindex routes
  under the worker threadpool (cross-thread SQLite regression guard).

### Configuration

All paths and endpoints are env-var driven with sensible defaults:
`KE_CORPUS_ROOT`, `KE_DATA_DIR`, `KE_REGISTRY_PATH`, `OLLAMA_BASE_URL`, plus
`KE_OLLAMA_URL`, `KE_EMBED_MODEL`, and `KE_EMBEDDINGS_DB` for the embedding
index. Cloud routing reads provider env-vars at startup
(`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, …); the cloud provider runs
read-only when no keys are present.

### Verified

- Clean-clone install simulation: `git clone` + `install` → editable package
  install succeeds.
- Dashboard end-to-end: every endpoint the UI calls returns 200 with no
  redirects; first-run banner renders.
- MCP server: `initialize` / `tools/list` / `tools/call search` all return
  valid JSON-RPC 2.0 responses.
- `pytest`: 4/4 pass.
