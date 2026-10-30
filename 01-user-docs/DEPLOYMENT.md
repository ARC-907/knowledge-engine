# Deployment

Knowledge-Engine is built **local-first**. The default deployment is your own
machine, and that is a fully supported, intended way to run it. This guide
covers running it beyond the laptop — as a service, on your LAN, in a
container — with the hardening that requires.

> **Read this first:** the engine ships as **local-trust**. The core routes
> (search, registry, generation) have no authentication; CORS is limited to
> loopback origins by default (`KE_CORS_ORIGINS` widens it); the agent board's
> admin routes require a key. It is safe on `localhost`. It is **not** safe
> exposed directly to the internet. Everything below about network exposure
> assumes you add the protections described here. See
> [`SECURITY.md`](../SECURITY.md) for the trust model and reporting.

## Deployment modes

| Mode | Who reaches it | Hardening needed |
| --- | --- | --- |
| **Localhost** (default) | Just you, on one machine | None — this is the safe default |
| **LAN / private network** | Your team, behind a firewall or VPN | Bind address + a reverse proxy |
| **Internet-exposed** | Anyone with the URL | Reverse proxy + auth + TLS — see below |

## Localhost (the default)

This is what [QUICKSTART.md](QUICKSTART.md) walks through:

```bash
uv run --project engine knowledge-engine serve
```

The server binds `127.0.0.1:9210` — reachable only from the same machine. For
single-user use, you are done.

## Running it as a long-lived service

For a corpus you query daily, you may want the engine to start with the machine
and stay up. Wrap the serve command in whatever your OS uses for long-lived
processes, with the `KE_*` variables in the unit's environment:

```bash
KE_HOME=/srv/knowledge-engine knowledge-engine serve --host 127.0.0.1 --port 9210
```

`knowledge-engine serve` runs uvicorn with the engine's logging configuration
and proxy-header policy. If you need uvicorn flags it does not expose, run
uvicorn directly with the same environment:

```bash
uvicorn knowledge_engine.app:create_app --factory --host 127.0.0.1 --port 9210
```

Under plain uvicorn, `--port` is the only way to change the port (`KE_PORT`
is read by `knowledge-engine serve`), and `KE_BIND_HOST` must be set to the
bind address if you also enable the MCP mount (below).

- **Linux:** a `systemd` user service or system unit, `KE_*` in
  `Environment=` lines.
- **macOS:** a `launchd` agent (`~/Library/LaunchAgents/`).
- **Windows:** a Scheduled Task set to run at logon, or a service wrapper such
  as NSSM. For the standalone agent board there is also
  `scripts/agent-board/board-watchdog.ps1` (see
  [`02-dev-docs/AGENT-BOARD.md`](../02-dev-docs/AGENT-BOARD.md)).

Keep `--host 127.0.0.1` unless you are deliberately exposing it (next section).

Logging: the servers log to stderr. `KE_LOG_LEVEL` (default `INFO`) sets the
level of the engine's own loggers; the startup lines `CORS: allow_origins=...`
and `board trust: ...` are emitted at INFO and are worth capturing in the
service log. `knowledge-engine -v serve` / `-q serve` override the level for
one run.

## LAN / team access

To let other machines on a trusted network reach the engine:

1. **Bind a reachable address.** Pass `--host 0.0.0.0` (all interfaces) or the
   specific LAN IP, and `--port <n>` (or `KE_PORT`) for a different port.
2. **Put a reverse proxy in front.** Do not expose uvicorn directly. Run nginx,
   Caddy or similar in front of it to terminate connections, add access
   controls and log requests. Set `KE_TRUST_PROXY=1` so the agent board sees
   real client addresses, and add the proxy's address to `KE_TRUSTED_PROXIES`
   if it is not on the same host.
3. **Restrict who can reach it.** A firewall rule, a private VPN (for example a
   mesh VPN), or the reverse proxy's allowlist — pick one. The engine itself
   does not gate access to the core routes.
4. **If you serve MCP over HTTP (`KE_MCP_HTTP=1`),** the `/mcp` endpoint checks
   the `Host` header. `knowledge-engine serve --host 0.0.0.0` tells it about
   the bind address; with plain uvicorn set `KE_BIND_HOST=0.0.0.0` too, or
   every non-loopback `Host` is refused with `421`. Set
   `KE_MCP_ALLOWED_HOSTS` to the names your proxy forwards (comma-separated;
   `*` disables the check) — see
   [MCP-WIRING.md → Streamable HTTP](MCP-WIRING.md#streamable-http).

For a team on a private VPN with no public endpoint, a reverse proxy plus the
VPN's own access control is a reasonable setup. The agent board trusts
loopback and the Tailscale range `100.64.0.0/10` by default; set
`KE_BOARD_TRUSTED_CIDRS` to your own networks otherwise.

## Internet exposure — the hardening checklist

If the engine must be reachable from the public internet, the engine alone is
not enough. You **must** add a layer in front of it:

- [ ] **Reverse proxy** (nginx / Caddy / Traefik) — never expose uvicorn
      directly.
- [ ] **TLS** — HTTPS only. Caddy does this automatically; nginx via
      Let's Encrypt.
- [ ] **Authentication** — the core routes have none. Add HTTP basic auth at
      the proxy, an OAuth proxy (e.g. oauth2-proxy), or an SSO gateway. Without
      this, anyone with the URL can read your entire corpus and toggle the
      registry.
- [ ] **Lock down write endpoints** — `POST /registry`, `PATCH
      /registry/{id}/toggle`, `DELETE /registry/{id}`,
      `PATCH /registry/lifecycle/state` and `POST /search/reindex` mutate
      state. At minimum require auth for them; if you only need read access,
      block them at the proxy.
- [ ] **CORS** — the app allows loopback origins only by default. If a browser
      UI is served from another origin, list it in `KE_CORS_ORIGINS`
      (comma-separated); `*` is accepted only when set explicitly. The
      dashboard at `/ui/` is same-origin and needs nothing. The CORS
      middleware is installed by `create_app()` in
      `engine/src/knowledge_engine/server/app.py`, using the shared helper in
      `core/http.py`.
- [ ] **Agent board** — admin routes need a key: run
      `knowledge-engine board keys bootstrap-master` once from the machine
      the engine runs on (inside the container for Docker, see below), keep
      the key out of the browser except in the dashboard's Config tab. Set
      `KE_BOARD_TRUSTED_CIDRS` to the networks that may read/post, and
      `KE_TRUST_PROXY=1` (plus `KE_TRUSTED_PROXIES` if the proxy is not on the
      same box) so the board sees real client addresses behind your reverse
      proxy. Without `KE_TRUST_PROXY=1` the server ignores `X-Forwarded-For`
      and `X-Forwarded-Proto` entirely, even from a proxy on the same host.
- [ ] **Rate limiting** — apply it at the proxy to blunt abuse and scraping.

The engine is open source (MIT) — if you need authentication *inside* the app
rather than at the proxy, you can add it. For 3.x, gating at the proxy is the
supported approach.

## Generation providers and secrets

The engine can route text generation (`POST /generate`) to providers. Three
are built in; `GET /generate/providers` lists them in selection order:

- **`echo`** — always available; returns the prompt. Useful for wiring tests.
- **`ollama`** — always registered; used when an Ollama server answers at
  `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`). Reachability is probed
  lazily and cached for `KE_PROVIDER_PROBE_TTL` seconds (default 30);
  unreachable providers are logged at startup (`KE_PROVIDER_STARTUP_PROBE`).
  `OLLAMA_MODEL` and `OLLAMA_TIMEOUT` set the default model and timeout.
- **`cloud-http`** — an OpenAI-compatible client, registered only when both
  `KE_CLOUD_ENDPOINT` (e.g. `https://api.example/v1`; the provider POSTs to
  `<endpoint>/chat/completions`) and `KE_CLOUD_API_KEY` are set. `KE_CLOUD_MODEL`
  is the default model, `KE_CLOUD_TIMEOUT` the request timeout (60 s). Upstream
  failures answer `502` on `/generate`.

`KE_PROVIDER_ORDER` reorders the providers. Secrets:

- Keep API keys in the environment or a gitignored `.env`
  (`<KE_PIPELINE_ROOT>/.env` is loaded at startup; variables already set win).
  **Never commit them.** `knowledge-engine config show` masks them; the engine
  warns when `KE_CLOUD_ENDPOINT` would send a key over plain `http` to a
  non-loopback host.
- The agent board's provider-credential registry stores only the *name* of the
  environment variable that holds a key (`KE_ANTHROPIC_API_KEY`,
  `KE_OPENAI_API_KEY`, `KE_CLOUD_API_KEY`, …), never the value.
- Every variable is listed in [CONFIGURATION.md](CONFIGURATION.md).

## Data, backups, and the index

- **Your corpus is the source of truth.** It is plain markdown — back it up
  like any other repo.
- **The indexes are derived data.** `<KE_DATA_DIR>/index.db` (FTS5) and
  `<KE_DATA_DIR>/embeddings.db` (if you built embeddings) can always be rebuilt
  with `knowledge-engine reindex` and `knowledge-engine embeddings build`. The
  default data dir is `<KE_HOME>/.knowledge-engine/data`.
- **The board and pipeline databases are state.** `<KE_DATA_DIR>/pipeline.db`
  (shared board, keys, pipeline tables) and `<KE_DATA_DIR>/board-scopes/*.db`
  hold messages and the key vault; back them up if you rely on the board.
- **The knowledge metadata is state.** `<KE_DATA_DIR>/knowledge.db`
  (`KE_KNOWLEDGE_DB`) holds workspaces, projects, collections and your
  classification of records; it cannot be rebuilt from the corpus. Back it up;
  `knowledge-engine knowledge migrate --apply` also takes a backup before it
  writes ([SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md)).
- `corpus/registry.json` *is* worth backing up — it records what is enabled.
  It is small and lives with the corpus.

## Docker

The repository root ships a `Dockerfile` and a `compose.yaml`. The image is a
multi-stage build: uv installs the locked dependency set and the engine into a
virtual environment, which is copied into a slim Python runtime. The server
runs as an unprivileged user (UID/GID 10001) on port 9210, and a
`HEALTHCHECK` polls `/health`.

Inside the container everything lives under `/data` (`KE_HOME=/data`, also the
working directory):

| Path | Contents | Set by |
| --- | --- | --- |
| `/data/corpus` | Your corpus, including `registry.json` — mount it | `KE_CORPUS_ROOT=/data/corpus` |
| `/data/.knowledge-engine/data` | FTS5 index, embeddings, pipeline/board databases | `KE_DATA_DIR` |

### Compose

```sh
docker compose up -d --build                                   # build + start
docker compose exec knowledge-engine knowledge-engine reindex  # build the index
curl http://127.0.0.1:9210/health
```

- `./corpus` is mounted **read-only** and must already contain
  `registry.json`; the engine writes a default registry when the file is
  missing, which fails on a read-only mount. Search, reindexing and the agent
  board work with a read-only corpus.
- Registry edits (`POST /registry`, toggle, delete, lifecycle changes,
  `knowledge-engine bootstrap`, `knowledge-engine watch`) write
  `registry.json` and need two things: a writable mount (drop `:ro` from the
  volume line) **and** a container user that may write to `./corpus`. The
  service runs as UID/GID 10001, so either build the image with your own ids
  (`KE_UID=$(id -u) KE_GID=$(id -g) docker compose build`) or
  `chown -R 10001:10001 corpus`. First run for a new corpus: mount it writable,
  run `docker compose exec knowledge-engine knowledge-engine bootstrap`, then
  switch back to `:ro`.
- The named volume `ke-data` holds `/data/.knowledge-engine`, so indexes and
  board databases survive restarts and image rebuilds. `docker compose down -v`
  deletes it; the index can always be rebuilt with `reindex`.
- **Board admin key:** `bootstrap-master` only answers loopback callers, and
  inside the container "loopback" means the container's own network namespace.
  Run it in the container, then read the key from the data volume:
  `docker compose exec knowledge-engine knowledge-engine board keys bootstrap-master`
  (the raw key is printed once and written to
  `/data/.knowledge-engine/data/board-master-key.txt`; delete that file after
  copying the key).
- The port is published on `127.0.0.1` only. The same rules as above apply to
  wider exposure: reverse proxy, authentication and TLS in front.
- The service runs with a read-only root filesystem, all Linux capabilities
  dropped, and `no-new-privileges`.

### Plain `docker`

```sh
docker build -t knowledge-engine:local .
docker run -d --name knowledge-engine -p 127.0.0.1:9210:9210 \
  -v "$PWD/corpus:/data/corpus:ro" \
  -v ke-data:/data/.knowledge-engine \
  knowledge-engine:local
docker exec knowledge-engine knowledge-engine reindex
```

The entrypoint is the `knowledge-engine` CLI and the default command is
`serve --host 0.0.0.0 --port 9210`, so any other subcommand can be run in
place of it — for example `docker run --rm -i -v "$PWD/corpus:/data/corpus:ro"
knowledge-engine:local mcp` for an MCP client that launches the stdio server.

- The index is built on demand: run `reindex` (CLI or `POST /search/reindex`)
  after the first start and whenever the corpus changes.
- The image's `HEALTHCHECK` always probes `http://127.0.0.1:9210/health`. When
  you run a non-serving subcommand (such as `mcp`) or change the port, disable
  it with `docker run --no-healthcheck` (compose: `healthcheck: {disable: true}`)
  or the container will be reported unhealthy.
- `VOLUME /data` means every `docker run` without `-v` for `/data` creates an
  anonymous volume. Use `--rm` for one-off commands and `docker rm -v` when
  removing a named container, so those volumes do not accumulate.
- Pass provider settings and other `KE_*` variables with `-e` / `--env-file`
  (or `environment:` / `env_file:` in compose). Never bake API keys into the
  image. To reach an Ollama server on the host, point every Ollama consumer at
  it: `OLLAMA_BASE_URL` (generation), `KE_OLLAMA_URL` (corpus embeddings) and,
  if you use the task classifier, `KE_CLASSIFIER_ENDPOINT` (for example
  `http://host.docker.internal:11434`, with
  `--add-host=host.docker.internal:host-gateway` on Linux).
- The base images can be overridden at build time with
  `--build-arg BUILDER_IMAGE=...` and `--build-arg RUNTIME_IMAGE=...` (for
  example to use a registry mirror); the user and group IDs with
  `--build-arg KE_UID=...` / `KE_GID=...`.

A container binds `0.0.0.0`. If it also serves MCP over HTTP (`KE_MCP_HTTP=1`),
the default `serve --host 0.0.0.0` command already tells the mount about the
bind address; set `KE_MCP_ALLOWED_HOSTS` to the hostnames the reverse proxy
forwards, otherwise `/mcp` accepts any `Host` and logs a warning at startup.

## Logs

The servers log to stderr. Two settings make the output easy to collect
(see [CONFIGURATION.md](CONFIGURATION.md)):

- `KE_LOG_FORMAT=json`: one JSON object per line (`ts`, `level`, `logger`,
  `message`, plus the exception text when there is one), including uvicorn's
  access log.
- `KE_LOG_FILE=/path/engine.jsonl`: also write to a size-rotated file
  (10 MiB, 5 backups).

`deploy/observability/` is a ready Compose stack with Loki, Grafana Alloy
(tailing the log file) and Grafana with a log dashboard; see
[DEV-TOOLING.md](../02-dev-docs/DEV-TOOLING.md#observability-loki-alloy-and-grafana).
It is sized for one machine; for a shared deployment, point Alloy at your
existing Loki.

## Upgrades

Knowledge-Engine is distributed as a repository and as GitHub Releases. To
take an update: pull the new version and re-run `uv sync --project engine`
(for a tool install, re-run `uv tool install --force` with the new tag), run
`knowledge-engine registry validate`, then `knowledge-engine reindex`. Your corpus and `registry.json` are yours and are
not overwritten by an engine update. Read the **Migrating** section of the
release in [`CHANGELOG.md`](../CHANGELOG.md) for each major version.

Existing pipeline and agent-board databases (`pipeline.db` and the per-scope
board files under the data directory) are upgraded in place the first time the
new version opens them: a `schema_version` table is added, any columns the
older layout lacked are created, and the applied versions are logged at INFO
(`adopted pre-versioned database ... at schema version N`). No manual step is
needed; back up the data directory before upgrading if you want a way back.
Scope database files whose names are not valid scope names (for example
`v1.2.db`) are listed but cannot be addressed; the engine logs a WARNING with
the suggested new name — rename them with the engine stopped.
