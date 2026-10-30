# Changelog

All notable changes to Knowledge-Engine are recorded here. The format follows
[Keep a Changelog 1.1](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) for the engine
package, the MCP tool surface and the registry schema. Compatibility shims for
renamed modules live for one major version (see `CONTRIBUTING.md`).

## [Unreleased]

### Added

- CI job `dashboard (browser smoke)` runs the Playwright dashboard tests on
  Ubuntu with the locked Playwright's Chromium
  ([#40](https://github.com/ARC-907/knowledge-engine/issues/40)). It fails if the
  tests are skipped or not collected, has a 15-minute timeout, and uploads a
  screenshot and the page's console errors for each failing test.

### Fixed

- The dashboard browser tests built their FTS index through `app.state`, which
  only exists inside the app lifespan since 3.0, so they errored before
  reaching the browser; they now build the index the way `reindex` does.

## [3.0.0] — 2026-10-24

The 3.0 release modernises the whole project: Python 3.12+, `uv` and
hatchling, a CI/release pipeline, the official MCP Python SDK, typed settings,
hardened defaults for the agent board and CORS, hybrid search, versioned
database migrations, a compiled dashboard, containers, and a documentation
refresh. Read **Migrating from 2.x** at the end of this section before
upgrading.

### Added

**Build, CI and release**

- Build backend hatchling; `requires-python >= 3.12`; PEP 735 dependency groups
  (`dev`, `lint`, `test`, `build`, `ui`); `uv.lock`; single version source via
  `importlib.metadata`.
- Ruff lint/format, mypy (strict on the core), coverage, pre-commit, `justfile`,
  `.editorconfig` / `.gitattributes`; repo-wide format sweep.
- CI matrix 3.12–3.14 (Ubuntu required; Windows/macOS advisory), build + wheel
  smoke, zizmor, CodeQL, OpenSSF Scorecard, release workflow (GitHub Release on
  `v*` tags with build provenance; PyPI job gated off), Dependabot (actions +
  uv, 7-day cooldown), CODEOWNERS, issue forms, PR template.
- `docs` CI job: `scripts/check_docs_paths.py` verifies that every
  `knowledge_engine.*` path and every relative link in the Markdown docs
  resolves (with its own tests); an offline `lychee` link check; a check that
  the generated `01-user-docs/CONFIGURATION.md` is current.
- Container support: multi-stage `Dockerfile` (uv builder →
  `python:3.12-slim-bookworm`, non-root UID 10001, `/data` volume, port 9210,
  `/health` healthcheck), `.dockerignore`, and `compose.yaml` (loopback-only
  port, read-only corpus mount, named volume for `/data/.knowledge-engine`,
  read-only rootfs, all capabilities dropped, no-new-privileges).
- `SECURITY.md` (supported versions, private reporting, trust model, hardening
  checklist), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `02-dev-docs/RELEASING.md`
  and architecture decision records under `02-dev-docs/adr/`.

**Paths and configuration**

- `KE_HOME` and `knowledge_engine.core.paths`, the single resolver for corpus,
  registry and data paths; `Config.ensure_dirs()`.
- `core.settings.Settings` (pydantic-settings): typed, documented settings for
  every environment variable; `knowledge-engine config show [--markdown]`;
  generated `01-user-docs/CONFIGURATION.md`.
- `KE_LOG_LEVEL` (default `INFO`) sets the level of the `knowledge_engine.*`
  loggers under `serve`, `board-serve`, `mcp` and `watch`;
  `knowledge-engine -v/-vv/-q/-qq` set the stderr log level of any command
  (one-shot commands default to `WARNING`).
- `KE_PROVIDER_ORDER`, `KE_PROVIDER_PROBE_TTL` (30 s),
  `KE_PROVIDER_STARTUP_PROBE`, `KE_CLOUD_TIMEOUT` (60 s).
- `KE_BOARD_STANDALONE_PORT` is honoured as `board-serve`'s fallback port;
  `knowledge-engine serve --port` defaults to `$KE_PORT`.

**HTTP API and server**

- Response models on every engine route; documented board request bodies;
  OpenAPI snapshot test (`engine/tests/snapshots/openapi.json`).
- `core/http.py` with the shared CORS helper and `BodySizeLimitMiddleware`.
- `KE_CORS_ORIGINS` (engine app CORS allow-list), `KE_TRUSTED_PROXIES` (peers
  allowed to set `X-Forwarded-For`), `KE_BOARD_MAX_SCOPES` (cap on scope
  databases, default 64).
- `PUT /board/scopes/{name}` (admin) creates a scope database explicitly;
  `GET /board/scopes` reports `max_scopes`;
  `knowledge-engine board scopes create NAME`.
- Startup logs of the effective CORS policy and board trust policy; INFO logs
  for every key create/revoke/toggle/grant; WARNING on every `bootstrap-master`
  call.
- `GET /search` returns `"syntax_fallback": true` (MCP `search` sets
  `_meta.syntax_fallback`) when a query that is not valid FTS5 syntax was
  searched as a phrase.

**MCP**

- MCP server rebuilt on the official MCP Python SDK (`mcp>=2.3,<3`), protocol
  `2026-07-28`; `initialize` clients still negotiate back to `2024-11-05`.
- `knowledge-engine mcp --http [--host] [--port]` serves MCP over streamable
  HTTP (loopback by default, DNS-rebinding protection; `KE_MCP_ALLOWED_HOSTS`
  adds Host names, `*` disables the check).
- `knowledge-engine mcp --selftest` builds the server, lists its tools over an
  in-process session, prints a JSON summary and exits 0 (packaging smoke test).
- `KE_MCP_HTTP=1` mounts the same MCP server at `/mcp` inside the FastAPI app,
  sharing its registry and indexer; `KE_BIND_HOST` (set by `serve --host`)
  selects the Host-header policy under plain uvicorn.
- `knowledge_engine.mcp.toolkit.register_group()` mounts the project-context
  and agent-board tool groups with their original names, descriptions and
  input schemas, validating arguments against the advertised schema.
- Optional `board_key` input on the MCP post tools (`board_post`, `board_claim`,
  `board_release`, `board_blocker`, `board_ack`).
- Frozen pre-migration tool snapshot (`engine/tests/snapshots/mcp_tools.json`)
  and an in-process MCP test suite.

**Registry and retrieval**

- `knowledge-engine registry validate [path] [--json]`: checks a registry file
  against the registry JSON Schema; exit 0 if valid, 1 if not, one line per
  error with its JSON pointer. The schema ships inside the package
  (`knowledge_engine/core/registry.schema.json`); `corpus/registry.schema.json`
  is an identical copy with a real `$id`, titles and descriptions.
- Hybrid search: `mode=fts|vector|hybrid` on `GET /search`, the MCP `search`
  tool and `knowledge-engine search --mode`; `hybrid` merges FTS5 and vector
  rankings with reciprocal rank fusion (k = 60). Default stays `fts`.
  Responses add `mode`; hits add `fused_score`, `vector_score`, `fts_score`.
- `knowledge-engine embeddings build|search|status` (`--provider
  fake|ollama|remote`, `--model`, `--db`, `--batch`, `--incremental`,
  `--rebuild`, chunking flags).
- `CorpusEmbeddingIndex` (`knowledge_engine.retrieval.embeddings`),
  `HybridSearcher` (`knowledge_engine.retrieval.hybrid`), `FakeProvider`
  (offline, deterministic).
- Settings: `KE_EMBED_PROVIDER` (ollama), `KE_EMBED_BATCH` (32),
  `KE_EMBED_REMOTE_URL`, `KE_EMBED_ALLOW_REMOTE` (off).
- `Indexer.search(..., kind=None)`; `Registry.counts()`, `upsert_many()`,
  `reload()`.

**Core and substrates**

- `core.util` helpers (time, hashing, slug, env, proc, sqlite, vectors) and
  `core.ollama.OllamaClient` (batched `/api/embed`).
- `core.util.sqlite` numbered-SQL migration runner (one transaction per
  migration, `schema_version`, optional migrations, adoption of pre-versioned
  databases) and `ConnectionCache` (per thread and path, bounded, closable from
  any thread).
- `project_context.db.apply_registry_migrations()` /
  `apply_project_migrations()`; `agent_board.digest`;
  `scopes.nonconforming_scopes()` and a startup WARNING for legacy scope
  database names.
- `runtime.foundation.config.load_dotenv_file()`.
- Optional hosted-tool fields `allow_inline`, `allowed_methods`,
  `allowed_headers` (a migration adds the columns; existing records keep
  working). Every tool invocation and proxy call is logged at INFO with
  credentials redacted.
- Tests: path resolution, `/ui`, version reporting, MCP kind filtering,
  corrupt leases and sweeper gating, real scanner discovery over
  `tests/fixtures/sample_project`, FTS5 missing-library warning, the queue,
  worker registry, task classifier, pipeline config loaders, tool host,
  migrations, connection lifecycle, concurrent WAL writers, a schema-upgrade
  fixture (`tests/fixtures/pipeline_db/legacy_inline_schema.sql`),
  property-based sanitizer tests; `pytest-repeat` in the test group.

**Dashboard**

- Light theme following the OS colour scheme, with a header toggle persisted
  in localStorage.
- Accessibility: viewport meta, landmarks, a real tab list with arrow-key
  navigation, labels on every control, visible focus rings, table captions.
- "API key" field in the Config tab; the value is kept in localStorage and
  sent as `X-Board-Key` on every `/board` request.
- `scripts/vendor.py` (stdlib, cross-platform) refreshes and SHA-384-verifies
  the vendored dashboard assets; `--check` verifies offline.
- `ui-src/` Tailwind v4 toolchain and `just ui-css` to rebuild the dashboard
  stylesheet; `just vendor`, `just vendor-check`, `just ui-test`.
- Opt-in Playwright smoke test (`ui` dependency group, `ui` marker,
  `KE_UI_TESTS=1`).

### Changed

**Configuration and paths**

- `Config.from_env()` has no filesystem side effects; `create_app`, the MCP
  server and the CLI create the data dir explicitly. A missing corpus root logs
  one WARNING. Blank `KE_*` path values count as unset.
- The pipeline DB, board scope DBs, master-key file and embeddings index all
  default to the same data dir.
- One boolean parser for all flags; unrecognised values log a WARNING and use
  the default.
- The pipeline `.env` (`$KE_PIPELINE_ROOT/.env`) is loaded explicitly at CLI /
  MCP / app startup instead of at import; `PIPELINE_ROOT` / `CONFIG_DIR`
  resolve lazily; `runtime.pipeline` submodules load lazily.
- Project-context Ollama embeddings default URL is `http://127.0.0.1:11434`
  (was `http://localhost:11434`).

**Server**

- `create_app(settings=None)` performs no I/O; the lifespan opens/closes the
  registry and FTS index, builds providers, runs the board `Sweeper` and the
  MCP mount. Routes use `Depends` providers.
- Provider availability is probed lazily and cached; Ollama is always
  registered (and listed) and skipped while unreachable.
- Board router moved to `agent_board/http/` (`server/api/board_routes.py` is an
  alias); board log records come from `knowledge_engine.agent_board.http`.
  Board trust / write / admin gates run as dependencies before body parsing
  (untrusted peers always get 403, keyless admins 401).
- Sweeper is a `Sweeper` object owned by each app; module-level functions
  remain.
- `X-Forwarded-For` is honoured only with `KE_TRUST_PROXY=1` and only from a
  trusted proxy; the chain is walked from the right instead of trusting the
  leftmost entry. With `KE_TRUST_PROXY` unset, `serve` / `board-serve` ignore
  `X-Forwarded-For` / `X-Forwarded-Proto` entirely, including from a same-host
  proxy; uvicorn's `FORWARDED_ALLOW_IPS` is no longer consulted — use
  `KE_TRUSTED_PROXIES`.
- Board request bodies are capped at 1 MiB for streamed/chunked bodies as well
  as declared `Content-Length` (413). With `require_key_for_post=1`, a key
  presented by a loopback caller is validated (loopback without a key stays
  allowed). Reads of an unknown scope return empty results instead of creating
  a database.
- Request-derived values (scope names, key labels, ids, header fields, request
  paths) are escaped before being written to logs (`core.logsafe.log_safe`).
- `verify_key` refreshes `last_used_at` at most once per 60 seconds per key.
- `registry.json` is written atomically and keeps its file mode; concurrent
  writes are serialised; registry mutations are copy-on-write and fsync'ed.
- `registry.json` is validated (Draft 2020-12) on every load and before every
  write. Problems raise `RegistryValidationError` listing every error, are
  logged at ERROR, and leave the file untouched; corpus CLI commands and
  `knowledge-engine mcp` exit 1 with a readable report instead of a traceback.

**MCP**

- MCP tool failures are returned as tool results with `isError: true` instead
  of JSON-RPC `-32602` errors; a failing tool can no longer kill the server.
- Base-tool input schemas are generated from typed signatures and carry
  pydantic `title`s / `anyOf[..., null]` (same names, parameters, enums,
  required fields and defaults as before).
- Optional tool groups that are disabled or fail to initialise are logged at
  WARNING and skipped; duplicate tool names across groups raise at
  registration.
- The MCP server logs to stderr at `KE_LOG_LEVEL` (default INFO); stdout is
  reserved for the transport. `knowledge-engine mcp --http --host
  <non-loopback>` logs a WARNING that the endpoint carries no authentication
  and no Host check unless `KE_MCP_ALLOWED_HOSTS` is set; `--host/--port`
  without `--http` is an error.
- `scripts/mcp-client-config.example.json` is valid JSON and portable; the
  primary launch command is the installed console script `knowledge-engine
  mcp`; the `uvx` form installs from the repository.
- Scanner MCP tools called without `project_fp` use the root project's
  registered fingerprint (`unknown_project` if unregistered; missing
  `branch_fp` on `scanner_ingest` → `invalid_args`).

**CLI**

- CLI split into the `knowledge_engine.cli` package with lazy per-command
  imports; commands, flags and output unchanged.
- The board CLI uses httpx: transport errors exit 2, HTTP errors exit 1 with
  the response body.
- Corpus and project-context embeddings use batched `/api/embed`; embeddings
  build progress is logged (stderr) instead of printed.

**Retrieval**

- FTS rebuild reads the corpus outside the index lock and swaps the table in
  one transaction; `index.db` uses WAL + `busy_timeout`.
- The embedding index records provider, model, dimension, chunking and
  timestamps, and refuses to mix models (`embedding_model_mismatch`, HTTP 409);
  `--rebuild` switches models. Embedding requests are batched across files; a
  failed chunk is retried by the next `--incremental` run; files removed from
  the corpus are pruned.
- One vector format everywhere (version byte + little-endian float32); stores
  written by older releases remain readable.
- `OllamaProvider` (project-context embeddings) is now `LocalOllamaProvider`
  (old name kept as an alias).

**Substrates**

- `runtime/foundation/db.py` is now the package `runtime/foundation/db/` with
  the schema in `db/schema/001–008_*.sql`; public names unchanged; existing
  pipeline and board databases upgrade in place on first open and gain a
  `schema_version` table.
- `agent_board/http.py` is now the package `agent_board/http/` (trust, limits,
  bodies, messages, admin); routes and OpenAPI unchanged.
- The project-context registry migration moved to `migrations/registry/`;
  migrations run in explicit transactions.
- `git_context.diff_summary` raises `ValueError` for unsafe refs. New
  redaction markers `<REDACTED:PRIVATE_KEY>`, `<REDACTED:OPENAI_API_KEY>`,
  `<REDACTED:GITHUB_TOKEN>`, `<REDACTED:SLACK_TOKEN>`.
- The FTS5 indexer's missing-library warning explains how to add the library
  to the corpus (or disable the entry). `sample_project` fixture uses
  "project-context" wording and includes a sanitized test log.

**Dashboard and scripts**

- Dashboard CSS is compiled and committed (`static/tailwind.css`, ~18 KB)
  instead of the 451 KB Tailwind Play-CDN runtime; no Node needed at runtime.
- Dashboard logic moved from inline `<script>` to `static/app.js`;
  initialisation hooks `alpine:init` instead of a timer; API calls are
  resolved relative to the page, so `/ui/` works behind a path prefix.
- `board-watchdog.ps1` resolves the interpreter from `engine/.venv` (or
  `uv run`), probes 127.0.0.1 for wildcard binds, only restarts the child it
  started (treating its own launcher process tree as its own), and writes
  `watchdog.pid` under the data dir.
- `.gitignore` rewritten with neutral, documented sections.

**Documentation**

- `CHANGELOG.md` lives at the repository root (`02-dev-docs/CHANGELOG.md` is a
  pointer). README and Quickstart use `uv sync` / `uv run knowledge-engine …`;
  every guide is refreshed for the 3.0 paths, commands, env vars and module
  names. `01-user-docs/CONFIGURATION.md` is generated from
  `knowledge_engine.core.settings`. GLOSSARY / ARCHITECTURE describe execution
  adapters generically; DEPLOYMENT documents Docker and the in-place database
  upgrade; AGENT-BOARD documents the 60 s `last_used_at` resolution.

### Deprecated

- `knowledge_engine.mcp_server` is a shim exposing only `main` and emits a
  `DeprecationWarning` on import (`python -m knowledge_engine.mcp_server`
  prints a notice on stderr); use `knowledge-engine mcp` or
  `knowledge_engine.mcp.server`.
- `python -m knowledge_engine.mcp.server` keeps working; the documented
  launch command is the console script `knowledge-engine mcp`.

### Removed

- Unused `local` and `mcp` extras; `pytest-asyncio`; `tomli` backport.
- `scripts/install.{sh,ps1}`, `scripts/serve.{sh,ps1}`,
  `scripts/update-vendor.{sh,ps1}`, `scripts/agent-board/serve-board.sh`,
  `scripts/agent-board/start-board.bat` — use `uv sync --project engine`,
  `uv run --project engine knowledge-engine serve|board-serve`,
  `python scripts/vendor.py` and `pwsh -File scripts/agent-board/board-watchdog.ps1`.
- `vendor/tailwindcss/3.4.16/tailwind.min.js` is no longer shipped.
- `engine/routing_local/` (broken import, no callers),
  `engine/sandbox_adapter/README.md`, `engine/data/.gitkeep`,
  `02-dev-docs/RESEARCH-LOG.md`.
- `ping` is not part of MCP protocol 2026-07-28 (SDK behaviour); use
  `knowledge-engine mcp --selftest` as the health check.
- MCP `search` accepts `limit` 1–100, the same bound as `GET /search`; a larger
  value is an `isError` result naming the bound.
- The standalone board's `/health` adds `"status": "ok"` (the engine's shape)
  next to the existing `"ok": true`.
- The per-thread pipeline/board connection cache holds up to 96 databases
  (was 32), so the sweeper keeps every scope open under the default
  `KE_BOARD_MAX_SCOPES` of 64.
- `[project.urls].Changelog` points at the root `CHANGELOG.md`.

### Fixed

- `knowledge-engine watch` honours the registry's lifecycle flags
  (`watch_enabled`, `auto_register`): it re-reads `registry.json` on every new
  folder, so `PATCH /registry/lifecycle/state` takes effect in a running
  watcher; with either flag off new folders are logged and not registered. A
  failing registration is logged and no longer stops the watcher thread.
- With no `KE_*` set, the corpus resolved to the nonexistent `engine/corpus`.
- `/ui` was never mounted (wrong path) and the dashboard was missing from
  wheels.
- MCP `search` applied `kind` after `limit` and returned too few results;
  `limit` must be ≥ 1.
- A corrupt `board.sweeper_lease` row raised `UnboundLocalError` on every
  sweep.
- Flaky `test_http_scope_isolation` (lifespan sweeper posted digests
  mid-test); tests no longer reference the stale `knowledge_engine.foundation.db`
  path.
- Admin routes were reachable without any key from any trusted peer under the
  default configuration.
- `key_hash` was returned by `GET /board/keys/{id}` and
  `PATCH /board/keys/{id}/toggle`.
- Master key file was briefly world-readable under a permissive umask; write
  failures were swallowed.
- Any trusted read with `?scope=<new>` created a new SQLite database on disk;
  production code special-cased the literal `testclient` host.
- A declared-Content-Length 413 from the board body cap now carries CORS
  headers for allowed origins (body-cap middleware mounted inside CORS).
- `cloud-http` is a real OpenAI-compatible client instead of a stub that echoed
  the prompt.
- `/registry?kind=bogus` → 422 (was 500); `/search` limit bounded to 1..100.
- Concurrent registry writes are serialised and atomic; SQLite connections are
  closed on shutdown.
- Ollama/cloud upstream failures → 502 `provider_error` (was 500); non-object
  board permission grants → 400 (was 500). `POST /board/sweep` responses no
  longer carry exception text.
- FTS5 syntax errors no longer silently return an empty result.
- `ToolContext` lazy connections are opened under a lock (concurrent MCP tool
  calls). `agent_board.cli` no longer reconfigures stdout/stderr at import.
- Cosine similarity no longer silently truncates vectors of different lengths;
  it raises. Embeddings paths and models resolve from Settings at call time
  instead of at import time. Docs and messages no longer reference the
  nonexistent `knowledge_engine.embeddings.*` modules.
- `semantic_search` / `similar_records` (project-context embeddings) skip
  stored vectors of another dimension with a WARNING instead of raising; the
  `embedding_model_mismatch` hint names `KE_EMBED_PROVIDER` as well when the
  index was built by another provider.
- HTTP error bodies for embedding failures no longer include the absolute path
  of `embeddings.db`; `knowledge-engine embeddings build|search|status` report
  a database that cannot be opened or written as `{"error": {"type":
  "io_error"}}` (exit 1); `knowledge_engine.retrieval.embeddings` re-exports
  the historical build functions lazily.
- Pipeline and board database connections opened on worker threads are closed
  on app, MCP, CLI and service shutdown; requests after shutdown get 503
  instead of 500.
- `KE_BOARD_MAX_SCOPES` can no longer be overshot by concurrent first posts
  within one process.
- The task classifier tolerates JSON answers that are not objects; non-mapping
  pipeline YAML gives a clear error.
- `knowledge-engine mcp` (stdio, `--http`, `--selftest`) creates `KE_DATA_DIR`
  before opening the registry and index databases.
- The Windows board watchdog never kills listeners it did not start.

### Security

- Engine CORS default is now loopback origins with explicit methods/headers;
  `*` only when set explicitly. `bootstrap-master` is loopback-only by socket
  peer, one-shot (409 afterwards), refuses `X-Forwarded-For`.
- Git refs supplied through the project-context MCP tools
  (`get_branch_lineage`, `get_change_context`, `search_by_diff`,
  `diff_summary`) are validated (`validate_ref`) and placed between
  `--end-of-options` and `--`, closing a git option-injection hole. All git
  access goes through one typed runner (`git_context.run_git`). Unsafe refs
  return the standard `invalid_args` error result.
- Pointer apply and rollback refuse to write outside the project root (path
  traversal, absolute targets, symlinks escaping the root) and report such
  items as structured errors; non-UTF-8 files are reported per item instead of
  crashing the run.
- Tool host: script tools must invoke `<interpreter> <script under the tool
  root>`; inline-code flags (`bash -c`, `python -c`/`-m`, `node -e`, stdin) are
  refused unless the tool record opts in with `allow_inline`. Service proxying
  only forwards allowlisted methods (default GET, POST) and headers (default
  content-type, accept), strips hop-by-hop headers and `Authorization` unless
  allowlisted, requires an http(s) upstream with a host, confines sub-paths,
  and only follows http(s) redirects.
- Sanitizer redacts PEM private-key blocks, `sk-` API keys, GitHub
  (`gh[pousr]_`, `github_pat_`) and Slack (`xox[baprs]-`) tokens, opaque
  `Bearer` tokens and JSON-quoted secret fields; sanitization is idempotent.
- Secrets are never logged or shown (`config show` masks them); warning when
  `KE_CLOUD_ENDPOINT` sends the key over plain http to a non-loopback host.
- `POST /generate` request bodies are capped at 1 MiB (413).
- Docker image runs unprivileged; compose hardens the container and binds to
  127.0.0.1 only; the build context excludes `.env*`, keys and secrets.
- Removed the `-ExecutionPolicy Bypass` launcher.

### **Breaking**

- Python 3.10 and 3.11 are no longer supported.
- Default data dir moved from `engine/data` (in practice `engine/src/data`
  since the `core/` move) to `<KE_HOME>/.knowledge-engine/data`. Set
  `KE_DATA_DIR` to keep the old location.
- Dashboard source moved from `ui/` to
  `engine/src/knowledge_engine/server/static/` (now shipped in wheels). The
  `/ui/` URL is unchanged; forks of `ui/index.html` must move their edits.
- Board admin routes return 401 without an `X-Board-Key` holding
  `board:admin`, including from loopback and regardless of
  `require_key_for_post`.
- Engine CORS no longer allows arbitrary origins.
- Scope names must match `^[a-z0-9][a-z0-9_-]{0,63}$`; others are rejected
  (400). Rename pre-3.0 scope files with dots or uppercase in their stem
  (`board-scopes/v1.2.db` → `v1-2.db`) before upgrading.
- `app.state.config/registry/indexer/providers` exist only while the lifespan
  runs (use `TestClient` as a context manager). Board trust and CORS settings
  are read once per app, not per request (restart after changing env vars).
- `/search?limit>100`, unknown registry kinds, `max_tokens<1` and an empty
  registry `id` now return 422. Boolean env spellings other than
  `1/true/yes/on` and `0/false/no/off` fall back to the default (were truthy).
- `/info` and `/generate/providers` always list `ollama`; with
  `KE_CLOUD_ENDPOINT` + `KE_CLOUD_API_KEY` set, `/generate` really calls that
  endpoint.
- `knowledge_engine.mcp.server` no longer exports `TOOLS`, `Server` or
  `PROTOCOL_VERSION`; use `build_server(config, registry, indexer)` and
  `server.list_tools()`. Default MCP protocol revision is `2026-07-28`; tool
  errors are `isError` results, not JSON-RPC errors. MCP `search` rejects an
  empty `kind` (`""`) like any other value outside the enum; omit `kind` or
  pass null for an unfiltered search.
- A registry that does not match the schema stops the engine at startup
  (unknown top-level keys, entries without `id`/`name`/`path`, known fields
  with the wrong type). Malformed JSON raises `RegistryValidationError` (a
  `ValueError`) instead of `json.JSONDecodeError`.
- `<KE_DATA_DIR>/registry.db` (the never-read SQLite mirror of
  `registry.json`) is no longer created or updated; an existing file can be
  deleted. `Registry(path, db_path)` still accepts and ignores `db_path`.
- `embeddings.db` from earlier releases (tables `files`/`chunks`, no model
  recorded) is not searchable; run `knowledge-engine embeddings build
  --rebuild`. `cosine_similarity` / `providers.cosine` raise
  `VectorDimensionError` (a `ValueError`) on mismatched lengths. Newly packed
  vector blobs are 4n+1 bytes (version byte).
- The install/serve shell scripts, the `local`/`mcp` extras and the vendored
  Tailwind runtime are gone (see **Removed**).

### Migrating from 2.x

1. **Python.** Install Python 3.12 or newer (`uv` fetches one for you), then
   `uv sync --project engine`. `pip install -e .[dev]` no longer works: `dev`
   is a dependency group, not an extra.
2. **Paths.** Runtime data now defaults to `<KE_HOME>/.knowledge-engine/data`,
   where `KE_HOME` is the nearest directory at or above the working directory
   containing `corpus/registry.json`. To keep using an existing
   `engine/data`, set `KE_DATA_DIR` to it, or run
   `knowledge-engine reindex` (and `embeddings build --rebuild`) once in the
   new location. Blank `KE_*` values now mean "unset". `registry.db` under the
   data dir is dead weight and can be deleted.
3. **Registry.** Run `knowledge-engine registry validate` before starting the
   engine: entries need `id`, `name` and `path`, and unknown top-level keys are
   rejected.
4. **Agent board keys.** Admin routes (keys, providers, config, sweep, scope
   creation) require a key with `board:admin` even from loopback. Run
   `POST /board/keys/bootstrap-master` (or `knowledge-engine board keys
   bootstrap-master`) from the host once, copy the key from
   `<KE_DATA_DIR>/board-master-key.txt`, delete the file, and pass the key as
   `X-Board-Key` (CLI `--key` / `KE_BOARD_KEY`; dashboard Config tab "API
   key"). Rename scope databases whose names do not match
   `^[a-z0-9][a-z0-9_-]{0,63}$`.
5. **CORS and proxies.** Browser UIs on other origins need `KE_CORS_ORIGINS`;
   behind a reverse proxy set `KE_TRUST_PROXY=1` (and `KE_TRUSTED_PROXIES` if
   the proxy is not on the same host). A TLS-terminating proxy on the same box
   must set `KE_TRUST_PROXY=1` too.
6. **MCP clients.** Launch with `knowledge-engine mcp` (the console script) or
   `python -m knowledge_engine.mcp.server`; `knowledge_engine.mcp_server` is
   deprecated. Tool failures now arrive as `isError` results rather than
   JSON-RPC errors; `TOOLS`, `Server` and `PROTOCOL_VERSION` are gone from
   `knowledge_engine.mcp.server`; `kind=""` is rejected (omit it instead);
   `ping` is gone, use `knowledge-engine mcp --selftest`.
7. **Scripts and extras.** Replace `scripts/install.*` with `uv sync --project
   engine`, `scripts/serve.*` with `uv run --project engine knowledge-engine
   serve`, `scripts/update-vendor.*` with `python scripts/vendor.py`, and
   `serve-board.sh` / `start-board.bat` with `knowledge-engine board-serve`
   or `pwsh -File scripts/agent-board/board-watchdog.ps1`. Drop the `local`
   and `mcp` extras from install commands.
8. **Embeddings.** An `embeddings.db` written by 2.x must be rebuilt:
   `knowledge-engine embeddings build --rebuild`.
9. **Embedding the app.** `create_app()` does no I/O; `app.state.*` exists only
   inside the lifespan, so test clients must be used as context managers.
   Restart the server after changing trust or CORS environment variables.

## [2.0.0] — 2026-06-14

### Changed (BREAKING) — `project_docs` subsystem renamed to `project_context`

Renamed for clarity (the subsystem indexes project *context* — code, docstrings,
comments, logs, git — not just "docs"):

- **MCP tools** `project_docs.*` → `project_context.*` (every tool in the group).
- **CLI** `knowledge-engine project-docs` → `knowledge-engine project-context`.
- **Config** `[project_docs]` TOML table → `[project_context]`; `ProjectDocsConfig`
  → `ProjectContextConfig`; `PROJECT_DOCS_DEFAULTS` → `PROJECT_CONTEXT_DEFAULTS`.
- **Default storage dir** `.knowledge-engine/project-docs` → `.knowledge-engine/project-context`.
- **Python package** `knowledge_engine.project_docs` → `knowledge_engine.project_context`.

SQLite **table names are intentionally unchanged** (`project_docs`,
`project_docs_fts`, `project_doc_bodies`, …) so existing per-project databases keep
working without a storage migration. The default storage *directory* changed —
set `[project_context].database_dir` to your old `.knowledge-engine/project-docs`
path if you have existing project DBs to retain.

### Changed — internal package layout (non-breaking)

Flattened the flat package into layered subpackages with no other public-surface
change: `core/` (config, registry, watcher), `server/` (app + `api/`), `mcp/`,
`generation/` (was `routing/`), `retrieval/` (`fts5.py` + `embeddings/`),
`tool_host/` (was `tools/`), `runtime/` (`foundation/` + `pipeline/` + `sandbox/`);
`agent_board/providers.py` → `provider_registry.py`. Root `app.py` / `mcp_server.py`
remain as compatibility shims, so `knowledge_engine.app` / `.mcp_server` import
paths still resolve.

## [1.2.0] — 2026-05-31

Two additive Agent Board capabilities on top of 1.1.0: physical
per-scope database segregation, and a provider-credential registry that
implements the original "config tab for provider-abstracted keys" spec.
Both ship empty and backward-compatible; the lean core is untouched.

### Added — Provider-credential registry (Agent Board)

A Config-tab surface for the *provider-abstracted keys* the engine uses to
reach upstream models — distinct from the board-ACCESS key vault that
authenticates callers *to* the board.

- **`agent_board/providers.py`** — CRUD over the (previously unused,
  always-empty) `api_keys` table. Each entry binds a provider
  (`anthropic` / `openai` / `cloud-http` / `ollama` / `custom`) to the
  **environment variable** that holds its secret. The DB stores the binding
  only — **never the credential** — honoring "no secrets in any committed
  file." `resolve_secret(provider)` returns the live value from the env for
  in-process engine use; it is never exposed by any list/HTTP read.
  `list_providers()` reports `env_set` (is the var populated?) without
  echoing the value.
- **Routes** — `GET/POST /board/providers`, `PATCH .../toggle`,
  `POST .../verify`, `DELETE .../{id}` (admin-gated).
- **Dashboard** — Config tab now has a **Provider credentials** section
  (provider, env-var name, status dot, verify/remove) above the renamed
  **Board access keys** section. The two systems were previously conflated
  under one mislabeled "Provider keys" heading; they're now clearly split.
- **Empty by default** — nothing seeds either table. Regression tests
  assert `GET /board/keys == []` and `GET /board/providers == []` on a
  fresh install, and that the provider list never leaks a secret value.
- **Engine integration point** — `routing/` and the classifier already read
  the same env vars the registry binds, so the registry is the single
  source of truth; wiring routing to consult it explicitly is the documented
  next hook.

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
- **Agent Board reference guide** ([`AGENT-BOARD.md`](02-dev-docs/AGENT-BOARD.md)) — channels, message types,
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
Alpine.js dashboard, and an optional bge-m3 embedding index. Ships with a demo
library and demo skill showing how to organize a knowledge corpus around a
domain-tuned three-lens framework so that AI agents can navigate it.

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
- **Demo corpus** (`corpus/samples/`) — a demo library (`demo-library`,
  three-lens CONCEPT → PATTERN → APPLY) and a demo skill (`demo-skill`),
  enabled in `corpus/registry.json` so `reindex` has real content on a
  fresh clone.
- **Documentation** — QUICKSTART, BRING-YOUR-OWN-CORPUS, LIBRARY-AUTHORING,
  SKILL-AUTHORING, MCP-WIRING, EMBEDDINGS, DEPLOYMENT, THEMING, FAQ
  (now under `01-user-docs/`).
- **Licensing** — MIT for the whole project (`LICENSE`).
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

[Unreleased]: https://github.com/ARC-907/knowledge-engine/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/ARC-907/knowledge-engine/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/ARC-907/knowledge-engine/compare/v1.2.0...v2.0.0
[1.2.0]: https://github.com/ARC-907/knowledge-engine/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/ARC-907/knowledge-engine/compare/428aee968c3e8df52f9ea56681c4d342da656936...v1.1.0
[1.0.0]: https://github.com/ARC-907/knowledge-engine/tree/428aee968c3e8df52f9ea56681c4d342da656936
