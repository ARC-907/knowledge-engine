# Changelog

All notable changes to the Knowledge-Engine.

## [Unreleased]

### Added — Per-scope database segregation (Agent Board)

The board can now give each **project / branch / agent / agentic loop** its
own physical SQLite database — a self-contained engine-block of board state
(messages, FTS index, key vault, config, sweeper lease) — while sharing one
process. This is the *physical* counterpart to the board's existing
*logical* segregation (channel / task_id / product_id / visibility_scope):
logical keeps everything co-queryable in one DB; a scope gives hard
separation for when an agent or tenant must not see another's traffic at all.

- **Foundation** (`foundation/db.py`) — a `contextvars.ContextVar` + the
  `using_db(path)` context manager let a block of work route every nested
  `get_connection()` (board, queue, key vault, sweeper) to a different
  database with no per-call argument threading. Resolution precedence is
  explicit-arg → scope-context → `KE_PIPELINE_DB` env → default.
- **Scope resolver** (`agent_board/scopes.py`) — maps a scope key to
  `<KE_DATA_DIR>/board-scopes/{slug}.db`. Keys are slugified (path-traversal,
  absolute paths, and reserved characters are neutralized). `list_scopes()`
  discovers existing scope DBs by directory scan.
- **Store** — every public read/write/config function takes an optional
  keyword-only `scope=`; supplying it runs the call (and its internal
  cross-calls) under `using_db`. `scope=None` (the default) uses the shared
  board — fully backward compatible.
- **HTTP** — `?scope=` query param on every data route (also accepted in the
  POST/ack/config body, body wins); new `GET /board/scopes` lists scope DBs.
- **MCP** — every read/post/search tool takes an optional `scope`; new
  `board_scopes` tool lists them.
- **CLI** — `--scope` flag on `read | post | search | digest | thread | ack`;
  new `board scopes` subcommand.
- **Sweeper** — one leased pass now sweeps the default board **and every
  scope DB** in turn, each under its own config + `board_sweeps` log. The
  process-wide lease (default DB) still prevents double-sweeps.

## [1.1.0] — 2026-05-30

Adds the **Agent Board** — a first-class, SQLite-backed coordination
surface (HTTP + MCP + CLI + dashboard) for agents collaborating across
worktrees, branches, research, planning, execution, and testing — plus a
provider-key vault, a background sweeper, a peer-trust gate (loopback +
Tailscale), and a root-cause fix making the pipeline DB path resolve
dynamically so the backbone is re-pointable at runtime. The lean core
(registry / FTS5 search / MCP / dashboard) is unchanged; the board is
net-new and opt-out via `KE_BOARD_ENABLED=0`.

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

### UX hardening pass (P3 papercuts)

Four items the original review filed as "P3 / cosmetic" turn out to be
the kind of papercut that costs a busy dev five minutes each — fixed:

- **FastAPI lifespan handler.** `@app.on_event("shutdown")` was
  deprecation-warning every test run. Replaced with an `asynccontextmanager`
  `_lifespan` in both `app.py` and `agent_board/service.py`. Test suite
  is now warning-clean.
- **FTS5 search auto-sanitize.** A user typing `foo (bar)` into the
  dashboard search box used to raise `sqlite3.OperationalError` and
  bubble up as a 500. `store.search_messages` now retries malformed
  queries as a phrase (`"foo (bar)"`) before falling back to LIKE —
  power users still get `*` / `AND` / `OR` / `NEAR()`, casual users
  never see a parse error.
- **CLI flag symmetry.** `--task` was silently ignored (different dest
  than `--task-id`); now a true alias. `--product` / `--product-id`
  the same. `board thread` accepts both a positional `correlation_id`
  AND a `--thread-id` flag so scripts using the long-lived thread id
  don't have to invent a fake correlation.
- **Last-master lockout protection.** `keys.toggle_key` and
  `keys.delete_key` now raise `LastMasterKeyError` (route → `409` with
  a recovery hint) if the operation would leave zero enabled master
  keys. The error message tells the operator exactly how to recover
  (`create another master first, OR delete this master directly in
  SQLite and re-run bootstrap-master`). `ensure_master_key` was
  already self-healing when no enabled master exists — verified with
  a new test covering the manual-delete recovery path. The route-level
  `409` translation is covered end-to-end (`PATCH` and `DELETE`) — see
  the dynamic-DB-path fix below for why those tests pass reliably now.

### Root-cause fix — dynamic pipeline DB path

`foundation/db.py` resolved the database path into a module-level
`DB_PATH` constant **at import time**. Any host that reconfigured
`KE_PIPELINE_DB` at runtime, ran two engines in one process, or
re-imported the package would have worker / sweeper threads silently
keep reading the *stale* database while writes went to the new one.
That fragility is incompatible with the design goal — a coordination
backbone you can hoist into any system and power up.

- `resolve_db_path()` now reads the environment on **every**
  `get_connection()`; connections are cached per-resolved-path in
  thread-local storage, so the dynamic read is one env lookup and a
  runtime DB switch routes correctly on every thread.
- `current_db_path()` exposes the live value; `DB_PATH` remains as the
  import-time default for back-compat.
- Regression guard `test_db_path_resolves_dynamically_per_request`
  flips `KE_PIPELINE_DB` mid-run and asserts the new connection sees the
  new DB with no row leakage from the old one.
- This is what let the two HTTP last-master `409` tests be restored
  rather than waved off — the FastAPI worker thread now reads the same
  database the request set up. The test harness dropped the 18-module
  re-import dance it used to need to fake this.

### Hardening pass (post-review)

- **Peer-trust gate.** `/board/*` now accepts loopback (`127.0.0.1`,
  `::1`) **plus** the Tailscale CGNAT range (`100.64.0.0/10`) by
  default; all other peers get `403` regardless of `require_key_for_post`.
  Override via `KE_BOARD_TRUSTED_CIDRS` (comma-separated; empty = loopback
  only). `X-Forwarded-For` is opt-in via `KE_TRUST_PROXY=1`. The
  `bootstrap-master` route refuses `X-Forwarded-For` outright and is
  loopback-only.
- **CORS lockdown.** Standalone service ships with a loopback CORS
  allowlist; override via `KE_BOARD_CORS_ORIGINS`. Restricted methods +
  headers (no `*`).
- **Atomic ack.** `store.ack_message` now wraps the read-modify-write
  in `BEGIN IMMEDIATE` so concurrent acks from different threads /
  clients can't clobber each other.
- **Sweeper lease.** Embedded + standalone sweepers coordinate via a
  `board.sweeper_lease` row in `kv_store`. Only the holder runs a pass;
  losers short-circuit and record the skip. Lease auto-expires so a
  crashed holder doesn't block the next window.
- **Sweeper SQL aggregates.** Reminder + digest dedup uses indexed
  `GROUP BY` queries instead of the per-pass `poll(500)` Python loop.
- **Sweeper hygiene.** Threshold-zero `stale_blocker_hours` is clamped
  to 1 (otherwise every blocker is "stale" → reminder spam with TTL=0).
  Digests exclude prior sweeper-posted digests so `top_senders` doesn't
  collapse to `board-sweeper`.
- **Master-key bootstrap.** Serialized by a module-level lock AND a
  unique partial index on `agent_api_keys(is_master) WHERE is_master=1
  AND enabled=1`, so two concurrent bootstrap calls can never both
  succeed. Best-effort `chmod 0600` on the master-key file on POSIX.
  `.gitignore` excludes `board-master-key.txt`.
- **prune_by_count.** Rewritten as bounded `IN` against the oldest
  overflow rather than correlated `NOT IN`. Unacked-blocker
  preservation predicate now parenthesized for clarity.
- **One-shot FTS5 backfill.** `_init_schema` no longer re-runs the
  backfill scan on every new thread-local connection — guarded by a
  module-level set keyed by DB path.
- **post_message inline thread_id.** `pipeline.message_board.post_message`
  accepts `thread_id` directly, dropping a round-trip from
  `store.post_with_validation`.
- **New composite indexes.** `idx_messages_type_created` and
  `idx_messages_reply_to` to support the sweeper's targeted aggregates.
- **Per-field length caps.** `subject` ≤ 500, identifier fields ≤ 100–200,
  `message_type`/`channel` ≤ 64, `ttl_hours` ≤ one year. 1 MiB hard cap
  on raw request body via the dependency layer (`413` before parse).
- **Shutdown hook.** FastAPI `on_event("shutdown")` stops the sweeper
  thread and releases its lease so peer sweepers (or the next reload)
  pick up immediately.
- **Naming + provenance cleanup.** `kb_*` aliases dropped — submodules
  are now imported under their actual names (`store`, `keys`,
  `sweeper`). Sibling-project references removed from source +
  docs + CHANGELOG (portfolio-grade hygiene).
- **__init__.py.** Submodules are now imported eagerly so
  `from agent_board import schemas` works without side-effect imports.

### Verified (post-hardening)

- `pytest`: 321 passed, 1 skipped, 0 failures.
- Test count for the board went from 18 → 36, adding coverage for:
  trust-gate (4 cases), atomic ack (2), master-key race + uniqueness (2),
  per-field caps + TTL cap + body-size cap (3), sweeper lease + force +
  threshold-clamp (4), HTTP untrusted-peer rejection (1),
  `prune_by_count` preservation (1).
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
  deployments on a separate port (default 11437 to avoid colliding with
  the engine's 9210).
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
