# Architecture

> How Knowledge-Engine is built and why. Read this after the README to
> understand what you can change, what you can replace, and how the pieces fit.

## Three layers

Knowledge-Engine is deliberately layered so each layer can be changed
independently of the others.

| Layer | Lives in | Replaceable? |
| --- | --- | --- |
| **Corpus** | `corpus/` | Yes — swap wholesale. Markdown content + the `registry.json` that lists it. |
| **Engine** | `engine/` | Open source (MIT). Fork it if you need to. Python package with its own `pyproject.toml`. |
| **Dashboard** | `ui/index.html` | Trivial fork. One Alpine.js + Tailwind HTML file, no build step. |

The corpus is data. The engine is code. The dashboard is a thin client over
the engine's HTTP API. You can replace any one without touching the others.

## Flexible-corpus principle

The engine **never hardcodes** library names, skill names, paths, or counts.
Every piece of discovery goes through the registry. `corpus/registry.json`
(mirrored into a queryable SQLite cache at `engine/data/registry.db`) is the
single source of truth for what is enabled and indexed.

This is what makes the engine corpus-agnostic: drop in any markdown corpus,
register it, reindex, and the dashboard and MCP server expose it without a
single code change.

## Component map

```
corpus/                         the knowledge content (data layer)
  registry.json                 source-of-truth list of libraries/skills/tools
  registry.schema.json          JSON Schema the registry is validated against
  libraries/                    domain libraries (3 demo libraries bundled)
  skills/  capabilities/  ...    other corpus surfaces (ship empty)
  schemas/  prompts/             bundled JSON schemas + prompt templates
  samples/                       demo-library + demo-skill (used by first-run + tests)

engine/                          the engine (code layer)
  pyproject.toml                 package metadata; `knowledge-engine` console script
  src/knowledge_engine/
    config.py                    env-driven paths (KE_CORPUS_ROOT, KE_DATA_DIR, ...)
    registry.py                  JSON registry + SQLite mirror; the source of truth
    indexer.py                   SQLite FTS5 indexer with bm25 ranking + snippets
    watcher.py                   watchdog-based auto-register of new corpus folders
    app.py                       FastAPI application factory
    cli.py                       `knowledge-engine` CLI (bootstrap/reindex/search/...)
    mcp_server.py                JSON-RPC 2.0 MCP stdio server
    api/                         FastAPI routes: health, registry, search, generate
    routing/                     provider abstraction: echo, cloud HTTP, Ollama
    embeddings/                  opt-in bge-m3 embedding index + cosine search
    sandbox/                     NoopSandbox scaffold + get_sandbox() factory hook
    foundation/                  opt-in pipeline foundation (YAML config loader + SQLite WAL backbone)
    pipeline/                    opt-in multi-worker pipeline (queue + message board + worker registry + task classifier)
    agent_board/                 first-class agent coordination surface — schemas, store, FTS5 search, keys, sweeper, CLI, MCP tool group
    tools/                       opt-in tool host (script/service/static tool registry over HTTP)
  routing_local/                 opt-in Ollama provider module
  sandbox_adapter/              opt-in sandboxed-agent scaffold (README only)
  tests/                         pytest smoke + regression tests

scripts/
  agent-board/                  optional standalone Agent Board service (watchdog + launchers)

ui/
  index.html                    single-file Alpine.js + Tailwind dashboard (Search / Registry / Board / Config tabs)
```

## HTTP API surface

The FastAPI app (`knowledge_engine.app:create_app`) serves:

| Route | Method | Purpose |
| --- | --- | --- |
| `/health` | GET | Liveness + version |
| `/info` | GET | Config, corpus counts, available providers, lifecycle flags |
| `/registry` | GET | List registry entries (`?kind=`, `?enabled_only=`) |
| `/registry/{id}` | GET | Fetch one entry |
| `/registry` | POST | Upsert an entry |
| `/registry/{id}/toggle` | PATCH | Enable/disable an entry |
| `/registry/{id}` | DELETE | Remove an entry |
| `/registry/lifecycle/state` | GET / PATCH | Read/update lifecycle flags |
| `/search` | GET | FTS5 search (`?q=`, `?limit=`) |
| `/search/reindex` | POST | Rebuild the FTS5 index |
| `/generate` | POST | Provider-routed text generation |
| `/generate/providers` | GET | List available generation providers |
| `/board/status` | GET | Agent board health + counts + last sweep |
| `/board/messages` | GET / POST | Poll + post messages (schema-validated) |
| `/board/messages/{id}` | GET | Fetch one |
| `/board/messages/{id}/ack` | POST | Acknowledge a `requires_ack` message |
| `/board/threads/{correlation_id}` | GET | Thread view, oldest-first |
| `/board/search` | GET | FTS5 search over subject+body |
| `/board/digest` | GET | Context-compressed summary (anti-context-overflow) |
| `/board/stats/{channels,types}` | GET | Per-channel / per-type counts |
| `/board/sweep` | POST | Manual sweeper trigger (admin) |
| `/board/keys`, `/board/keys/{id}`, `/board/keys/{id}/permissions` | * | Provider-key vault (admin) |
| `/board/config` | GET / PATCH | Singleton runtime config (port, sweeper, channels) |
| `/ui/` | GET | The single-file dashboard |
| `/docs` | GET | OpenAPI / Swagger UI (provided by FastAPI) |

The default deployment is **local-trust** — CORS is open and there is no
authentication for the `/search`, `/registry`, and `/generate` surfaces.
Harden before exposing the server beyond localhost.

The `/board/*` surface adds its own peer-trust gate: loopback and the
Tailscale CGNAT range (`100.64.0.0/10`) are accepted by default, all
other peers get 403. Override the trusted set with `KE_BOARD_TRUSTED_CIDRS`
and require an `X-Board-Key` for non-loopback writes by flipping
`require_key_for_post=1` in the board config. Full trust model in
[`docs/AGENT-BOARD.md`](docs/AGENT-BOARD.md).

## MCP discovery surface

`engine/src/knowledge_engine/mcp_server.py` is a JSON-RPC 2.0 server speaking
MCP protocol `2024-11-05` over stdio. It exposes four tools to AI assistants
(Claude Desktop, Cursor, Continue, any MCP client):

- `search(query, limit=10, kind=None)` — full-text search across enabled
  libraries / skills / tools. Pass `kind` to scope to one entry type.
- `registry_list(kind=None, enabled_only=False)` — list registry entries.
- `registry_toggle(entry_id, enabled)` — enable or disable an entry.
- `registry_get(entry_id)` — fetch a single registry entry.

Plus the agent-board tool group (auto-discovered via
`agent_board/mcp_tools/` — 14 tools total):

- **post** (5): `board_post`, `board_claim`, `board_release`,
  `board_blocker`, `board_ack`.
- **read** (7): `board_read`, `board_relevant`, `board_thread`,
  `board_digest`, `board_status`, `board_channels`,
  `board_message_types`. `board_digest` is the **context-saver** —
  returns counts + recent subjects instead of full bodies, so an agent
  catching up doesn't flood its context window.
- **search** (1): `board_search` — FTS5 over the board with bm25
  ranking and snippets.
- **sweep** (1): `board_sweep_now` — manual sweeper trigger.

When you add a new corpus surface, expose it through MCP — the MCP tool list
is what an AI assistant sees, so anything not on it is invisible to agents.

## Concurrency note

`Registry` and `Indexer` each hold a long-lived SQLite connection. Because the
FastAPI app runs synchronous route handlers on a worker threadpool, those
connections are opened with `check_same_thread=False` and every cursor
operation is serialized behind a lock. This lets a single shared `Registry` /
`Indexer` live on `app.state` and be used safely from any worker thread. If
you add a new SQLite-backed component, follow this pattern — do not share a
bare connection across threads.

## Optional layers (loud escape hatches)

Several subsystems are **opt-in** and the lean core works without them:

- `embeddings/` — bge-m3 semantic search via Ollama. Build with
  `python -m knowledge_engine.embeddings.build`.
- `routing_local/` — the Ollama provider for local model routing.
- `sandbox_adapter/` — a scaffold for sandboxed-agent execution; ships as a
  README only. Bring your own executor.
- `foundation/` — YAML config loader (`config.py`) + SQLite WAL backbone
  (`db.py`). The shared base for the pipeline and tools layers. Lives under
  `$KE_PIPELINE_ROOT` (default: `./pipeline`); SQLite at `$KE_PIPELINE_DB`
  (default: `$KE_DATA_DIR/pipeline.db`).
- `pipeline/` — coordinated multi-worker pipeline on top of `foundation/`:
  `queue` (lease-based SQLite task queue), `message_board` (append-only
  coordination channel), `worker_registry` (heartbeat tracking + auto-release
  of stale claims), `task_classifier` (tiny model that classifies incoming
  tasks by domain/complexity). Buyers running coordinated agent pipelines
  on top of the engine wire this in; the lean-core happy path never touches it.
- `agent_board/` — first-class agent coordination surface that promotes
  `pipeline/message_board` into a fully-tooled product: schema-validated
  channels + types (`schemas.py`), FTS5 search + context-compressed digest
  (`store.py`), background sweeper for TTL + stale-blocker reminders
  (`sweeper.py`), provider-key vault (`keys.py`), HTTP routes
  (`api/board_routes.py`), CLI (`knowledge-engine board ...`), and an
  auto-discovered MCP tool group (`agent_board/mcp_tools/`). Optional
  standalone watchdog mode lives in `scripts/agent-board/` for headless
  deploys. Buyer-facing guide: [`docs/AGENT-BOARD.md`](docs/AGENT-BOARD.md).
  Opt-out via `KE_BOARD_ENABLED=0`.
- `tools/` — `host.py` registers addressable tools that agents discover and
  invoke over HTTP. Three tool kinds: `script` (run a command), `service`
  (proxy to an upstream HTTP service), `static` (serve a file or directory).
  Built on `foundation/db.py`.

If an optional layer's dependencies are absent at install time, the layer
degrades gracefully — it is never allowed to silently break the lean core.

## Dependency isolation

`DEPENDENCIES.md` carries the full mapping of required-core vs.
optional-feature vs. dev-only dependencies, and which launcher enables each.
