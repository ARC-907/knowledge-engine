# Architecture

> How Knowledge-Engine is built and why. Read this after the README to
> understand what you can change, what you can replace, and how the pieces fit.

The proposed next development sequence and contracts are in
[ROADMAP.md](ROADMAP.md) and [V2-DESIGN.md](V2-DESIGN.md). Those documents
separate planned broker/module/citation/Serena capabilities from the behavior
implemented here.

## Three layers

Knowledge-Engine is deliberately layered so each layer can be changed
independently of the others.

| Layer | Lives in | Replaceable? |
| --- | --- | --- |
| **Corpus** | `corpus/` | Yes — swap wholesale. Markdown content plus the `registry.json` that lists it. |
| **Engine** | `engine/` | Open source (MIT). Fork it if you need to. Python package with its own `pyproject.toml` and lockfile. |
| **Dashboard** | `engine/src/knowledge_engine/server/static/` | Trivial fork. `index.html` + `app.js` (Alpine.js) + a compiled Tailwind stylesheet; rebuilding the CSS is the only build step, and only after markup changes. |

The corpus is data. The engine is code. The dashboard is a thin client over
the engine's HTTP API. You can replace any one without touching the others.

## Flexible-corpus principle

The engine **never hardcodes** library names, skill names, paths, or counts.
Every piece of discovery goes through the registry. `corpus/registry.json` is
the single source of truth for what is enabled and indexed: it is validated
against a JSON Schema on every load and write, rewritten atomically on every
change, and there is no SQLite copy of it.

This is what makes the engine corpus-agnostic: drop in any markdown corpus,
register it, reindex, and the dashboard and MCP server expose it without a
single code change.

An empty install can therefore have zero user-supplied skills, kits, tools,
or hosted tools while the substrate for those objects is still present. Treat
"empty" as a data state, not as a missing feature
(`knowledge-engine capabilities` reports both).

## Capability and retrieval model

Knowledge Engine is a scoped capability and memory runtime. Retrieval is not a
single table or route; it is the combined discovery, search, open, and execute
surface over several source-owned systems:

| Source | Owns | Retrieval/execution surface |
| --- | --- | --- |
| Base corpus registry | Libraries, skills, kits, tools | `/search` (`fts` / `vector` / `hybrid`), registry routes, base MCP `search` / `registry_*` tools |
| Project context | Project/lane docs, logs, git context, pointers, embeddings | `project_context.*` MCP tools and `knowledge-engine project-context ...` CLI |
| Agent board | Scoped agent messages, digests, blockers, acknowledgements | `/board/*`, board CLI, `board_*` MCP tools |
| Hosted tools | Script/service/static tool registrations | `tool_host/host.py` backed by `runtime.foundation.db` |
| Foundation | Shared substrate: SQLite backbone, queues, worker registry, tool records, board config | `runtime/foundation/` and the optional `runtime/pipeline/` modules |
| Execution adapter | Execution delegated to an external or sandboxed agent runtime | `runtime/sandbox/` factory hook (`get_sandbox()`); bring your own executor |

A future retrieval broker should normalise and govern these existing surfaces.
It should not flatten all stores into one database. Each source keeps
ownership of its full/original content and exposes allowed open modes through
adapters.

## Component map

```
corpus/                            the knowledge content (data layer)
  registry.json                    source-of-truth list of libraries/skills/kits/tools (+ lifecycle flags)
  registry.schema.json             JSON Schema the registry is validated against (same rules as core/registry.schema.json)
  samples/                         demo-library + demo-skill (enabled by default; used by first-run + tests)

engine/                            the engine (code layer): pyproject.toml, uv.lock, tests/
  src/knowledge_engine/
    cli/                           `knowledge-engine` CLI: parser + lazy dispatch (__init__.py); corpus, registry,
                                   embeddings, serve, mcp, board, project_context command modules
    app.py  mcp_server.py          compatibility shims (server.app.create_app / mcp.server.main; mcp_server is deprecated)
    core/                          paths.py (KE_HOME + KE_* resolver), settings.py (typed Settings, config show, CONFIGURATION.md
                                   generator), config.py, registry.py + registry.schema.json, watcher.py, http.py (CORS, body cap,
                                   proxy policy), logging_config.py, logsafe.py, errors.py, ollama.py, util/ (env, hashing, proc,
                                   slug, sqlite [migration runner, ConnectionCache], time, vectors)
    server/                        app.py (I/O-free FastAPI factory), lifespan.py, deps.py, schemas.py, api/ (health, registry,
                                   search, generate, board alias), static/ (dashboard served at /ui/)
    mcp/                           server.py (official MCP Python SDK; stdio + streamable HTTP), http.py (/mcp mount, Host policy),
                                   options.py, tool_groups.py, toolkit.py (legacy tool-group adapter)
    generation/                    base.py (Provider, ProviderRegistry, cached probes), cloud_http.py, ollama_provider.py, factory.py
    retrieval/                     fts5.py (FTS5 index), ownership.py (nested-entry ownership), embeddings/ (CorpusEmbeddingIndex, providers, build/search), hybrid.py (RRF)
    project_context/               per-project memory: config, db + migrations/, registry, fingerprints, ingest, search, pointers,
                                   git_context, logs, sanitize, scanner/, embeddings/, mcp_tools/ (46 tools), cli
    agent_board/                   schemas, store, digest, scopes, keys, provider_registry, sweeper, http/ (trust, limits, bodies,
                                   messages, admin), service (standalone app), cli, mcp_tools/ (15 tools)
    tool_host/                     host.py — script/service/static tool registry
    runtime/                       foundation/ (config.py YAML + .env loader; db/ versioned SQLite backbone, schema/001–008_*.sql),
                                   pipeline/ (queue, message_board, worker_registry, task_classifier), sandbox/ (NoopSandbox hook)
  tests/                           pytest suite; snapshots/ (cli_help.txt, openapi.json, mcp_tools.json); fixtures/

ui-src/                            Tailwind v4 toolchain that produces server/static/tailwind.css (dev-time only)
scripts/                           check_docs_paths.py, vendor.py, mcp-client-config.example.json, agent-board/ (Windows watchdog)
Dockerfile  compose.yaml           container image + compose stack
```

## Process model

`knowledge_engine.server.app.create_app(settings=None)` builds the FastAPI app
without touching the filesystem or the network: it reads a `Settings`
instance (every environment variable, typed), installs the body cap and CORS
middleware, mounts the board router, the optional MCP endpoint (`KE_MCP_HTTP=1`)
and the dashboard, and registers the lifespan. The **lifespan** does the I/O:
it loads `<KE_PIPELINE_ROOT>/.env` (variables already set win), creates the data
directory, opens the registry and the FTS index, builds the provider registry,
starts the board `Sweeper` and the MCP session manager, and closes all of them
on shutdown. Routes obtain these objects through `Depends` providers
(`server/deps.py`); `app.state.*` exists only while the lifespan runs, which is
why tests use `TestClient` as a context manager.

The `knowledge-engine` CLI (`cli/__init__.py`) configures one stderr log
handler, loads the same `.env` first thing, and then imports only the module of
the subcommand it runs. `serve` hands the factory to uvicorn with the engine's
logging configuration and the proxy-header policy derived from
`KE_TRUST_PROXY` / `KE_TRUSTED_PROXIES`. Under plain `uvicorn --factory` the
`.env` is loaded by the lifespan only, after `Settings` was built, so values
that exist only in that file reach the substrate modules that read the
environment directly but not the typed settings — export them in the real
environment when you bypass the CLI.

Logging: library modules only call `logging.getLogger(__name__)`. One-shot
commands log at WARNING, long-running ones (`serve`, `board-serve`, `mcp`,
`watch`) at `KE_LOG_LEVEL` (INFO); `-v`/`-q` override both. Request-derived
values are escaped before they are logged (`core/logsafe.py`).

## HTTP API surface

The FastAPI app (`knowledge_engine.app:create_app`) serves the routes below;
`engine/tests/snapshots/openapi.json` pins the OpenAPI document.

| Route | Method | Purpose |
| --- | --- | --- |
| `/health` | GET | Liveness + version |
| `/info` | GET | Version, resolved paths, corpus counts, providers in selection order, lifecycle flags |
| `/registry` | GET | List registry entries (`?kind=`, `?enabled_only=`); unknown kinds → 422 |
| `/registry/{entry_id}` | GET | Fetch one entry |
| `/registry` | POST | Upsert an entry (validated against the schema) |
| `/registry/{entry_id}/toggle` | PATCH | Enable/disable an entry |
| `/registry/{entry_id}` | DELETE | Remove an entry |
| `/registry/lifecycle/state` | GET / PATCH | Read/update lifecycle flags |
| `/search` | GET | Search (`?q=`, `?limit=` 1–100, `?mode=fts\|vector\|hybrid`, default `fts`; `syntax_fallback` flag) |
| `/search/reindex` | POST | Rebuild the FTS5 index |
| `/generate` | POST | Provider-routed text generation (body capped at 1 MiB; upstream failure → 502) |
| `/generate/providers` | GET | Registered generation providers in selection order |
| `/board/status`, `/board/channels`, `/board/message_types` | GET | Board health + counts, configured channels, canonical types |
| `/board/messages` | GET / POST | Poll + post messages (schema-validated); `?scope=` on every data route |
| `/board/messages/{message_id}` | GET | Fetch one |
| `/board/messages/{message_id}/ack` | POST | Acknowledge a `requires_ack` message |
| `/board/threads/{correlation_id}` | GET | Thread view, oldest-first |
| `/board/search` | GET | FTS5 search over subject+body |
| `/board/digest` | GET | Context-compressed summary |
| `/board/stats/channels`, `/board/stats/types` | GET | Per-channel / per-type counts |
| `/board/scopes` | GET | List per-scope databases (+ `max_scopes`) |
| `/board/scopes/{name}` | PUT | Create a scope database (admin) |
| `/board/sweep` | POST | Manual sweeper trigger (admin) |
| `/board/keys`, `/board/keys/{key_id}`, `/board/keys/{key_id}/toggle`, `/board/keys/{key_id}/permissions`, `/board/keys/permissions/{perm_id}` | * | Board access-key vault (admin) |
| `/board/keys/bootstrap-master` | POST | Create the first master key (loopback socket peer only, once) |
| `/board/providers`, `/board/providers/{key_id}`, `…/toggle`, `…/verify` | * | Provider credential bindings (admin) |
| `/board/config` | GET / PATCH | Singleton runtime config (PATCH is admin) |
| `/mcp` | * | MCP streamable HTTP, only with `KE_MCP_HTTP=1` |
| `/ui/` | GET | The dashboard |
| `/docs` | GET | OpenAPI / Swagger UI (provided by FastAPI) |

The default deployment is **local-trust**: the server binds `127.0.0.1`, CORS
allows loopback origins only (`KE_CORS_ORIGINS` widens it; `*` only when
explicit), and the `/search`, `/registry` and `/generate` surfaces have no
authentication. Harden before exposing the server beyond localhost
([`01-user-docs/DEPLOYMENT.md`](../01-user-docs/DEPLOYMENT.md), [`SECURITY.md`](../SECURITY.md)).

The `/board/*` surface adds its own gates, evaluated as dependencies before
the body is parsed: a peer-trust gate (loopback and the Tailscale CGNAT range
`100.64.0.0/10` by default; `KE_BOARD_TRUSTED_CIDRS` replaces the list; all
other peers get 403), a write gate (`require_key_for_post`), and an admin gate
(`X-Board-Key` with `board:admin`, always). Full trust model and route
inventory in [`AGENT-BOARD.md`](AGENT-BOARD.md).

## MCP surface

`engine/src/knowledge_engine/mcp/server.py` builds the server on the official
MCP Python SDK. It speaks protocol `2026-07-28` (clients using the `initialize`
handshake negotiate back to `2024-11-05`) over stdio (`knowledge-engine mcp`),
its own streamable-HTTP listener (`--http`, port 9211) or the `/mcp` mount on
the API server. `build_server(config, registry, indexer)` returns the server;
`--selftest` lists its tools over an in-process session. Tool failures are
returned as `isError` tool results; a failing tool never takes the server down.

Base tools, generated from typed signatures:

- `search(query, limit=10, kind=None, mode="fts")` — search enabled entries;
  `kind` scopes to one entry type (omit or pass `""` for all), `mode`
  selects `fts` / `vector` / `hybrid`; `_meta.syntax_fallback` reports a query
  that was retried as a phrase.
- `registry_list(kind=None, enabled_only=False)` — list registry entries.
- `registry_toggle(entry_id, enabled)` — enable or disable an entry.
- `registry_get(entry_id)` — fetch a single registry entry.

Optional groups are mounted by `mcp/toolkit.py` with their original names,
descriptions and input schemas (arguments are validated against the advertised
schema); a group that is disabled or fails to initialise is logged and skipped.
`engine/tests/snapshots/mcp_tools.json` freezes the wire schemas.

- **project-context** (`project_context/mcp_tools/`, 46 tools): capability
  (`project_context.capabilities`, `config_status`, `healthcheck`,
  `explain_available_tools`), query/retrieval (`search`, `get_record`,
  `get_summary`, `get_full_content`, `search_by_path`, `search_by_type`,
  `search_by_branch`, `search_recent`), pointers (`resolve_pointer`,
  `list_pointers`, `validate_pointer`, `pointer_backrefs`), git/log/test
  context (`git_context`, `search_by_commit`, `get_branch_lineage`,
  `get_change_context`, `explain_file_history`, `search_by_diff`,
  `search_test_logs`, `search_build_logs`, `search_runtime_logs`,
  `get_test_history`, `get_failure_context`, `get_latest_test_summary`),
  embeddings (`embedding_status`, `generate_embeddings`, `refresh_embeddings`,
  `semantic_search`, `similar_records`, `cluster_records`), identity/scanner
  (`list_projects`, `register_project`, `validate_project`, `list_branches`,
  `resolve_fingerprint`, `current_context`, `scanner_report`,
  `scanner_ingest`, `scanner_status`, `scanner_validate`,
  `scanner_plan_pointers`, `scanner_apply_pointers`).
- **agent-board** (`agent_board/mcp_tools/`, 15 tools): post (`board_post`,
  `board_claim`, `board_release`, `board_blocker`, `board_ack`; optional
  `board_key`), read (`board_read`, `board_relevant`, `board_thread`,
  `board_digest` — the context-saver that returns counts and subjects instead
  of bodies, `board_status`, `board_channels`, `board_message_types`), scope
  (`board_scopes`), search (`board_search`), sweep (`board_sweep_now`).

When you add a new corpus surface, expose it through MCP — the MCP tool list
is what an AI assistant sees, so anything not on it is invisible to agents.

## Concurrency and storage

`Registry` holds the JSON document in memory and `Indexer` holds a long-lived
SQLite connection. Because the FastAPI app runs synchronous route handlers on a
worker threadpool, each serialises its state behind a lock: every registry
mutation (copy-on-write, validated, written atomically and fsync'ed) runs
under the registry lock, and every FTS cursor operation under the indexer lock
— `rebuild()` reads the corpus first and only swaps the table contents under
it. SQLite connections are opened through `core.util.sqlite.connect` (WAL, 5 s
busy timeout, foreign keys, `check_same_thread=False`). This lets a single
shared `Registry` / `Indexer` live on `app.state` and be used safely from any
worker thread. If you add a new SQLite-backed component, follow this pattern —
do not share a bare connection across threads.

Every on-disk location derives from `core/paths.py`: `KE_HOME` (default: the
nearest directory at or above the working directory with
`corpus/registry.json`), `KE_CORPUS_ROOT`, `KE_REGISTRY_PATH`, and
`KE_DATA_DIR` (default `<KE_HOME>/.knowledge-engine/data`), which holds
`index.db` (FTS5), `embeddings.db`, `pipeline.db` (board, keys, pipeline
tables) and `board-scopes/*.db`. Schema changes to the pipeline/board and
project-context databases are numbered SQL migrations run by
`core.util.sqlite` (one transaction each, recorded in `schema_version`;
pre-versioned databases are adopted in place).

## Optional layers (loud escape hatches)

Several subsystems are **opt-in** and the lean core works without them:

- `retrieval/embeddings/` — the corpus embedding index
  (`CorpusEmbeddingIndex`; bge-m3 via Ollama by default, `remote` and `fake`
  providers) and `retrieval/hybrid.py` (`vector` / `hybrid` search modes,
  reciprocal rank fusion). Build with `knowledge-engine embeddings build`; see
  [`01-user-docs/EMBEDDINGS.md`](../01-user-docs/EMBEDDINGS.md).
- Ollama — every Ollama HTTP call (the `ollama` generation provider, corpus and
  project-context embeddings, the pipeline task classifier) goes through one
  client, `core/ollama.py` (`OllamaClient`: `available()`, `generate()`,
  batched `embed()`). Each consumer keeps its own variables (`OLLAMA_BASE_URL`
  / `OLLAMA_MODEL` / `OLLAMA_TIMEOUT`, `KE_OLLAMA_URL` / `KE_EMBED_MODEL` /
  `KE_EMBED_BATCH`, the project-context `[project_context.embeddings]` table,
  `KE_CLASSIFIER_ENDPOINT` / `KE_CLASSIFIER_MODEL`); the module docstring
  lists the precedence.
- `runtime/sandbox/` — the execution-adapter hook. `get_sandbox()` returns a
  `NoopSandbox` (every `run(command, stdin=None, timeout=30.0)` answers
  `SandboxResult(returncode=127)`); to delegate heavy or containerised work
  (Docker, WSL, a microVM, a subprocess jail), return your own object with the
  same `run()` signature from that factory. Disabled by default.
- `runtime/foundation/` — YAML config loader (`config.py`, with
  `load_dotenv_file()`) + the SQLite backbone (`db/`). The shared base for the
  pipeline, board and tool-host layers. Lives under `$KE_PIPELINE_ROOT`
  (default `./pipeline`, resolved when first needed, not at import); the
  database at `$KE_PIPELINE_DB` (default `$KE_DATA_DIR/pipeline.db`). The DB
  path is resolved **dynamically** on every `get_connection()`, and a
  `using_db(path)` context routes nested calls to another file — this is how
  board scopes work. Connections are cached per thread and path and closed on
  shutdown.
- `runtime/pipeline/` — coordinated multi-worker pipeline on top of the
  foundation: `queue` (lease-based SQLite task queue), `message_board`
  (append-only coordination channel), `worker_registry` (heartbeats +
  auto-release of stale claims), `task_classifier` (small model classifying
  tasks by domain/complexity). Teams running coordinated agent pipelines wire
  this in; the lean-core happy path never touches it.
- `agent_board/` — the coordination surface built on `runtime/pipeline/message_board`:
  schema-validated channels + types (`schemas.py`), FTS5 search +
  context-compressed digest (`store.py`, `digest.py`), background sweeper for
  TTL + stale-blocker reminders (`sweeper.py`), access-key vault (`keys.py`),
  provider credential bindings (`provider_registry.py`), HTTP routes
  (`agent_board/http/`; `server/api/board_routes.py` is an alias), CLI
  (`knowledge-engine board ...`), MCP tool group (`agent_board/mcp_tools/`) and
  a standalone service (`service.py`, `knowledge-engine board-serve`;
  Windows watchdog in `scripts/agent-board/`). Opt out with `KE_BOARD_ENABLED=0`.
  Supports **per-scope physical database segregation**: pass a `scope`
  (project / branch / agent / loop) and the board routes that request to
  `<KE_DATA_DIR>/board-scopes/{name}.db`; omit it for the shared board. One
  leased sweeper pass covers the default DB plus every scope. Reference guide:
  [`AGENT-BOARD.md`](AGENT-BOARD.md).
- `tool_host/` — `host.py` registers addressable tools that agents discover
  and invoke: `script` (run an allowlisted interpreter on a script under the
  tool root), `service` (proxy to an upstream HTTP service with method/header
  allowlists), `static` (serve a file or directory). Built on
  `runtime/foundation/db/`.

If an optional layer's dependencies are absent or a layer fails to initialise,
it degrades loudly (a WARNING and a skipped mount) — it is never allowed to
silently break the lean core.

## Dependency isolation

[`DEPENDENCIES.md`](DEPENDENCIES.md) carries the full mapping of runtime
dependencies, optional external software and development groups. Design
decisions with lasting consequences are recorded in [`adr/`](adr/).
