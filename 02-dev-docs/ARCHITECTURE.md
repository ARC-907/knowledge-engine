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
| Scoped-knowledge metadata | Workspaces, projects, sources, collections, record classification (V2 P0/P1) | `/knowledge/*`, `knowledge_*` MCP tools, `knowledge-engine knowledge ...`; no effect on `/search` |
| Retrieval broker | Scoped search/open across the corpus and linked project context (V2 P2) | `POST /knowledge/search`, `/knowledge/open`, `knowledge.search` / `knowledge.open` MCP tools, `knowledge-engine knowledge search\|open` |
| Evidence and citations | Immutable revisions, retained bytes, aliases, typed evidence spans, derivations, tombstones; exact-revision open; claim validation and citation export (V2 P3/P4) | `knowledge-engine knowledge snapshot`, `/knowledge/evidence/open`, `/knowledge/citations/validate\|export`, `knowledge_evidence_*` / `knowledge_cite_*` MCP tools, `knowledge-engine knowledge evidence\|cite ...` |
| Modules | Operator-registered executable utilities (script, service, external MCP) behind manifest v1, `modules.db` and one dispatcher (V2 P5) | `/modules/*` (board admin key), `<module_id>.<tool>` MCP tools, `knowledge-engine module ...` |

The versioned contracts (query context, hits, envelopes, source status,
locators, typed errors, the `SourceAdapter` protocol) live in
`knowledge_engine.knowledge`; the scope rules are ADR 0006 and the identity
rules ADR 0007 (see [`adr/`](adr/README.md)). The retrieval broker
(`knowledge_engine.knowledge.broker`, ADR 0011) resolves the scope from one
`knowledge.db` snapshot, runs one adapter per selected source
(`knowledge_engine.knowledge.sources`: the corpus FTS/embedding indexes and
linked project-context stores; the board is not a source) concurrently under
the request deadline, rechecks visibility, deduplicates and fuses the
per-source rankings by reciprocal rank. Operator guide:
[`01-user-docs/KNOWLEDGE-SEARCH.md`](../01-user-docs/KNOWLEDGE-SEARCH.md).
The evidence store (`knowledge_engine.knowledge.evidence`, ADR 0012) records
source bytes as content-addressed revisions in `knowledge.db`; `open_evidence`
is the exact-revision open path, and the broker's corpus adapter falls back to
it when a requested revision is no longer the file's current content (the
retained bytes are returned as `stale`, never the newer file). Citations
(ADR 0013) are identified by evidence identity, never by row ids.
The broker normalises and governs these existing surfaces.
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
                                   embeddings, serve, mcp, board, project_context, knowledge command modules
    app.py  mcp_server.py          compatibility shims (server.app.create_app / mcp.server.main; mcp_server is deprecated)
    core/                          paths.py (KE_HOME + KE_* resolver), settings.py (typed Settings, config show, CONFIGURATION.md
                                   generator), config.py, registry.py + registry.schema.json, watcher.py, http.py (CORS, body cap,
                                   proxy policy), logging_config.py, logsafe.py, errors.py, ollama.py, util/ (env, hashing, proc,
                                   slug, sqlite [migration runner, ConnectionCache], time, vectors)
    server/                        app.py (I/O-free FastAPI factory), lifespan.py, deps.py, schemas.py, api/ (health, registry,
                                   search, generate, knowledge, board alias), static/ (dashboard served at /ui/)
    mcp/                           server.py (official MCP Python SDK; stdio + streamable HTTP), http.py (/mcp mount, Host policy),
                                   options.py, tool_groups.py, toolkit.py (legacy tool-group adapter)
    generation/                    base.py (Provider, ProviderRegistry, cached probes), cloud_http.py, ollama_provider.py, factory.py
    retrieval/                     fts5.py (FTS5 index), ownership.py (nested-entry ownership), embeddings/ (CorpusEmbeddingIndex, providers, build/search), hybrid.py (RRF)
    project_context/               per-project memory: config, db + migrations/, registry, fingerprints, ingest, search, pointers,
                                   git_context, logs, sanitize, scanner/, embeddings/, mcp_tools/ (46 tools), cli
    agent_board/                   schemas, store, digest, scopes, keys, provider_registry, sweeper, http/ (trust, limits, bodies,
                                   messages, admin), service (standalone app), cli, mcp_tools/ (15 tools)
    tool_host/                     host.py — script/service/static tool registry; run_bounded (argv, own process group,
                                   deadline, output cap, cancellation) is the module execution substrate
    modules/                       V2 modules: manifest.py (+ manifest.schema.json), catalog.py + migrations/ (modules.db),
                                   dispatcher.py (ModuleHost: lifecycle + the one invocation path), transports.py
                                   (script/service/external MCP), artifacts.py, ingestion.py (P3 interface + test double),
                                   mcp_tools.py (`<module_id>.<tool>` group), config.py
    knowledge/                     V2 contracts and metadata: contracts.py (versioned Pydantic models, locators,
                                   identity helpers), errors.py (stable codes), adapter.py (SourceAdapter protocol),
                                   scope.py (scope resolution + reference filter), store.py + migrations/ (knowledge.db),
                                   backfill.py (`knowledge migrate`), service.py (admin operation catalog), mcp_tools.py,
                                   config.py; broker.py (retrieval broker), broker_api.py (request models shared by
                                   HTTP/MCP/CLI), broker_mcp.py (`knowledge.search`/`knowledge.open`), sources/
                                   (corpus and project-context adapters, default adapter factory);
                                   evidence/ (V2 P3/P4: policy.py retention/deletion, extract.py typed
                                   locators, store.py revisions + idempotent ingestion + outbox, resolve.py exact-revision
                                   open, citations.py claim validation + Markdown/CSL-JSON, snapshot.py `knowledge snapshot`)
    runtime/                       foundation/ (config.py YAML + .env loader; db/ versioned SQLite backbone, schema/001–008_*.sql),
                                   pipeline/ (queue, message_board, worker_registry, task_classifier), sandbox/ (NoopSandbox hook)
  tests/                           pytest suite; snapshots/ (cli_help.txt, openapi.json, mcp_tools.json,
                                   knowledge_contracts.json); fixtures/

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
| `/knowledge/status` | GET | Scoped-knowledge counts, schema version, last migration run |
| `/knowledge/workspaces` | GET / POST | List / create workspaces |
| `/knowledge/workspaces/{workspace_id}/projects` | GET / POST | List / create projects (display names may repeat) |
| `/knowledge/workspaces/{workspace_id}/sources` | GET | Record-owning sources |
| `/knowledge/workspaces/{workspace_id}/collections` | GET / POST | List (`?project_id=`) / create collections |
| `/knowledge/collections/{collection_id}/members`, `…/members/remove` | POST | Add / remove records (many-to-many) |
| `/knowledge/records/{source_id}/{record_id}` | PATCH | Research type, curation status, tags, project |
| `/knowledge/records/query` | POST | Records in a scope (`QueryContext` semantics, ADR 0006) |
| `/knowledge/workspaces/{workspace_id}/profiles`, `…/profiles/{profile_id}` | GET / PUT | Scope profiles |
| `/knowledge/workspaces/{workspace_id}/project-context/links/{project_id}` | PUT / DELETE | Link / unlink a project to a project-context store |
| `/knowledge/search` | POST | Retrieval broker: scoped search across sources (`SearchEnvelope`) |
| `/knowledge/open` | GET / POST | Retrieval broker: open an in-scope record at an exact revision (`OpenResult`) |
| `/knowledge/records/{source_id}/{record_id}/revisions` | GET | Revisions (oldest first), current revision, aliases, deletion state |
| `/knowledge/evidence/open` | POST | Open the exact cited revision/span after a scope recheck: `verified`, `stale`, `unavailable`, `ambiguous`, `unsupported` |
| `/knowledge/citations/validate` | POST | Validate a claim-to-evidence map (not proof of entailment) |
| `/knowledge/citations/export` | POST | Markdown references and/or CSL-JSON, deduplicated by evidence identity |
| `/modules`, `/modules/discover` | GET | Registered modules (intent + observed state); manifests under `KE_MODULES_DIR` (board admin key) |
| `/modules/register` | POST | Register a directory of `KE_MODULES_DIR` by name |
| `/modules/{module_id}` | GET | Module status and recent sanitized runs |
| `/modules/{module_id}/configuration` | PUT | Operator settings and secret references |
| `/modules/{module_id}/enable`, `…/disable`, `…/health` | POST | Enable (checks), disable (drain/cancel), bounded health probe |
| `/modules/{module_id}/tools/{tool}/invoke` | POST | Invoke through the shared dispatcher (scope, effects, ingest) |
| `/modules/jobs`, `/modules/jobs/{job_id}`, `…/cancel` | GET / GET / POST | Long-running module jobs, scoped by workspace |
| `/mcp` | * | MCP streamable HTTP, only with `KE_MCP_HTTP=1` |
| `/ui/` | GET | The dashboard |
| `/docs` | GET | OpenAPI / Swagger UI (provided by FastAPI) |

The default deployment is **local-trust**: the server binds `127.0.0.1`, CORS
allows loopback origins only (`KE_CORS_ORIGINS` widens it; `*` only when
explicit), and the `/search`, `/registry`, `/knowledge` and `/generate`
surfaces have no authentication. `/knowledge` is mounted unless
`KE_KNOWLEDGE_ENABLED=0`; its failures are typed
(`{"detail", "type", "error": {"code", ...}}`, codes in
`knowledge_engine.knowledge.errors`). Harden before exposing the server beyond localhost
([`01-user-docs/DEPLOYMENT.md`](../01-user-docs/DEPLOYMENT.md), [`SECURITY.md`](../SECURITY.md)).

The `/board/*` surface adds its own gates, evaluated as dependencies before
the body is parsed: a peer-trust gate (loopback and the Tailscale CGNAT range
`100.64.0.0/10` by default; `KE_BOARD_TRUSTED_CIDRS` replaces the list; all
other peers get 403), a write gate (`require_key_for_post`), and an admin gate
(`X-Board-Key` with `board:admin`, always). Full trust model and route
inventory in [`AGENT-BOARD.md`](AGENT-BOARD.md). Every `/modules/*` route
uses the same peer-trust + admin gate (mounted unless `KE_MODULES_ENABLED=0`).

Modules (`knowledge_engine.modules`, ADR 0009, 0014, 0015) are the only path
from a request to executing operator-installed code. A module's manifest is
validated as data on register; nothing runs until an operator configures and
enables it, and a module directory inside the corpus is refused. The MCP
`modules` group advertises the tools of enabled modules once per server
process. Module processes are **not sandboxed**: they run with the engine's
OS user, without its environment (only `PATH`, locale, temp dirs and their
declared secret references), in their own process group. See
[`01-user-docs/MODULES.md`](../01-user-docs/MODULES.md).

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

- **knowledge** (`knowledge/mcp_tools.py`, 20 tools, unless
  `KE_KNOWLEDGE_ENABLED=0`): one `knowledge_<operation>` tool per entry of the
  admin operation catalog in `knowledge/service.py` (`status`,
  `workspace_list`/`_create`, `project_list`/`_create`, `source_list`,
  `collection_list`/`_create`/`_add`/`_remove`, `record_classify`,
  `record_query`, `profile_list`/`_set`, `project_context_link`/`_unlink`,
  `evidence_open`, `evidence_revisions`, `cite_validate`, `cite_export`), the
  same operations as the `/knowledge` routes and the `knowledge` CLI. The store opens on the first
  call; typed failures are `isError` results carrying `{"error": {...}}`.
- **knowledge_broker** (`knowledge/broker_mcp.py`, 2 tools, unless
  `KE_KNOWLEDGE_ENABLED=0`): `knowledge.search` and `knowledge.open`, the same
  request models and JSON as `POST /knowledge/search` / `/knowledge/open` and
  `knowledge search|open`. The broker (its own registry, FTS index, embedding
  index and `knowledge.db` handles) opens on the first call.

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
tables), `knowledge.db` (scoped-knowledge metadata and the evidence store:
revisions, retained bytes, spans, tombstones; `KE_KNOWLEDGE_DB`) and
`board-scopes/*.db`. Schema changes to the pipeline/board,
project-context and knowledge databases are numbered SQL migrations run by
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
