# Deployment

Knowledge-Engine is built **local-first**. The default deployment is your own
machine, and that's a fully supported, intended way to run it. This guide
covers running it beyond the laptop — on a server, on your LAN, with the
hardening that requires.

> **Read this first:** the engine ships as **local-trust**. The core
> routes (search, registry, generation) have no authentication; CORS is
> limited to loopback origins by default (`KE_CORS_ORIGINS` to change it);
> the agent board's admin routes require a key. It is safe on `localhost`.
> It is **not** safe exposed directly to the internet. Everything below
> about network exposure assumes you add the protections described here.
> See [`SECURITY.md`](../SECURITY.md) for the trust model and reporting.

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
single-user use, you are done. Nothing further is required.

## Running it as a long-lived service

For a corpus you query daily, you may want the engine to start with the machine
and stay up.

The engine runs under uvicorn:

```
uvicorn knowledge_engine.app:create_app --factory --host 127.0.0.1 --port 9210
```

with `KE_CORPUS_ROOT`, `KE_DATA_DIR`, and `KE_REGISTRY_PATH` exported (the
`serve` scripts do this). Wrap that command in whatever your OS uses for
long-lived processes:

- **Linux:** a `systemd` user service or system unit running the uvicorn
  command, with the `KE_*` vars in the unit's `Environment=` lines.
- **macOS:** a `launchd` agent (`~/Library/LaunchAgents/`).
- **Windows:** a Scheduled Task set to run at logon, or a service wrapper such
  as NSSM.

Keep `--host 127.0.0.1` unless you are deliberately exposing it (next section).

## LAN / team access

To let other machines on a trusted network reach the engine:

1. **Bind a reachable address.** Change `--host 127.0.0.1` to `--host 0.0.0.0`
   (all interfaces) or to the specific LAN IP. Pass `--port <n>` (or set
   `KE_PORT`) if you want a different port.
2. **Put a reverse proxy in front.** Don't expose uvicorn directly. Run nginx,
   Caddy, or similar in front of it to terminate connections, add access
   controls, and log requests.
3. **Restrict who can reach it.** A firewall rule, a private VPN (e.g. a mesh
   VPN), or the reverse proxy's allowlist — pick one. The engine itself does
   not gate access.
4. **If you serve MCP over HTTP (`KE_MCP_HTTP=1`),** the `/mcp` endpoint checks
   the `Host` header. `knowledge-engine serve --host 0.0.0.0` tells it about
   the bind address; with plain uvicorn set `KE_BIND_HOST=0.0.0.0` too, or
   every non-loopback `Host` is refused with `421`. Set
   `KE_MCP_ALLOWED_HOSTS` to the names your proxy forwards (comma-separated;
   `*` disables the check) — see
   [MCP-WIRING.md → Streamable HTTP](MCP-WIRING.md#streamable-http).

For a team on a private VPN with no public endpoint, a reverse proxy plus the
VPN's own access control is a reasonable setup.

## Internet exposure — the hardening checklist

If the engine must be reachable from the public internet, the engine alone is
not enough. You **must** add a layer in front of it:

- [ ] **Reverse proxy** (nginx / Caddy / Traefik) — never expose uvicorn
      directly.
- [ ] **TLS** — HTTPS only. Caddy does this automatically; nginx via
      Let's Encrypt.
- [ ] **Authentication** — the engine has none. Add HTTP basic auth at the
      proxy, an OAuth proxy (e.g. oauth2-proxy), or an SSO gateway. Without
      this, anyone with the URL can read your entire corpus and toggle the
      registry.
- [ ] **Lock down write endpoints** — `POST /registry`, `PATCH
      /registry/.../toggle`, `DELETE /registry/...`, and `POST
      /search/reindex` all mutate state. At minimum, require auth for them; if
      you only need read access, block them at the proxy.
- [ ] **CORS** — the app allows loopback origins only by default. If a
      browser UI is served from another origin, list it in
      `KE_CORS_ORIGINS` (comma-separated); `*` is accepted only when set
      explicitly. The dashboard at `/ui` is same-origin and needs nothing.
- [ ] **Agent board** — admin routes need a key: run
      `knowledge-engine board keys bootstrap-master` on the host once and
      keep the key out of the browser. Set `KE_BOARD_TRUSTED_CIDRS` to the
      networks that may read/post, and `KE_TRUST_PROXY=1` (plus
      `KE_TRUSTED_PROXIES` if the proxy is not on the same box) so the
      board sees real client addresses behind your reverse proxy. Without
      `KE_TRUST_PROXY=1` the server ignores `X-Forwarded-For` and
      `X-Forwarded-Proto` entirely, even from a proxy on the same host.
- [ ] **Rate limiting** — apply it at the proxy to blunt abuse and scraping.

The engine is open source (MIT) — if you need real auth *inside* the app rather
than at the proxy, you can add it. But for v1.0, gating at the proxy is the
supported approach.

## Generation providers and secrets

The engine can route text generation to providers. If you wire those up:

- Provider API keys (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, etc.) are read
  from the environment at startup. **Never commit them.** Use your platform's
  secret store, or a `.env` file that is gitignored (`.env` is already in
  `.gitignore`).
- If no provider keys are set, the cloud provider runs read-only and the echo
  provider is always available — the engine still serves search and the
  dashboard fine.
- The Ollama provider is used automatically if Ollama is reachable at
  `OLLAMA_BASE_URL`; no key needed.

## Data, backups, and the index

- **Your corpus is the source of truth.** It's plain markdown — back it up like
  any other repo (git, your normal backup).
- **The indexes are derived data.** `<KE_DATA_DIR>/index.db` (FTS5; default
  data dir `<KE_HOME>/.knowledge-engine/data`) and
  `embeddings.db` (if you built embeddings) can always be rebuilt with
  `knowledge-engine reindex` and the embeddings `build` command. You do not
  need to back them up — though backing them up saves a rebuild on restore.
- `corpus/registry.json` *is* worth backing up — it records what's enabled.
  It's small and lives with the corpus.

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
| `/data/.knowledge-engine/data` | FTS5 index, registry cache, agent-board databases | `KE_DATA_DIR` |

### Compose

```sh
docker compose up -d --build                                   # build + start
docker compose exec knowledge-engine knowledge-engine reindex  # build the index
curl http://127.0.0.1:9210/health
```

- `./corpus` is mounted **read-only** and must already contain
  `registry.json`; the engine writes a default registry when the file is
  missing, which fails on a read-only mount. Search, reindexing, and the agent
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
- The port is published on `127.0.0.1` only. The same rules as above apply to
  wider exposure: reverse proxy, authentication, and TLS in front.
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
use `knowledge-engine serve --host 0.0.0.0` or set `KE_BIND_HOST=0.0.0.0`
alongside plain uvicorn, and set `KE_MCP_ALLOWED_HOSTS` to the hostnames the
reverse proxy forwards; otherwise `/mcp` only accepts loopback `Host` headers
and answers `421` to everything else.

## Upgrades

Knowledge-Engine is distributed as a repository. To take an update: pull the
new version (or re-download), re-run the install script to pick up any new
dependencies, and run `knowledge-engine reindex`. Your corpus and
`registry.json` are yours and are not overwritten by an engine update.
