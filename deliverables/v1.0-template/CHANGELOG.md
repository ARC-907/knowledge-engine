# Changelog

All notable changes to the Knowledge-Engine.

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
