# Deployment

Knowledge-Engine is built **local-first**. The default deployment is your own
machine, and that's a fully supported, intended way to run it. This guide
covers running it beyond the laptop — on a server, on your LAN, with the
hardening that requires.

> **Read this first:** the engine ships as **local-trust**. There is no
> authentication and CORS is fully open. It is safe on `localhost`. It is
> **not** safe exposed directly to the internet. Everything below about
> network exposure assumes you add the protections described here.

## Deployment modes

| Mode | Who reaches it | Hardening needed |
| --- | --- | --- |
| **Localhost** (default) | Just you, on one machine | None — this is the safe default |
| **LAN / private network** | Your team, behind a firewall or VPN | Bind address + a reverse proxy |
| **Internet-exposed** | Anyone with the URL | Reverse proxy + auth + TLS — see below |

## Localhost (the default)

This is what the `serve` scripts do, and what [QUICKSTART.md](QUICKSTART.md)
walks through:

```pwsh
.\scripts\serve.ps1     # Windows
./scripts/serve.sh      # macOS / Linux
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
   (all interfaces) or to the specific LAN IP. Set `KE_PORT` if you want a
   different port.
2. **Put a reverse proxy in front.** Don't expose uvicorn directly. Run nginx,
   Caddy, or similar in front of it to terminate connections, add access
   controls, and log requests.
3. **Restrict who can reach it.** A firewall rule, a private VPN (e.g. a mesh
   VPN), or the reverse proxy's allowlist — pick one. The engine itself does
   not gate access.

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
- [ ] **Tighten CORS** — the app sets `allow_origins=["*"]`. If the dashboard
      is served from a known origin, restrict CORS to it (edit
      `engine/src/knowledge_engine/app.py`, the `CORSMiddleware` block).
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
- **The indexes are derived data.** `engine/data/index.db` (FTS5) and
  `embeddings.db` (if you built embeddings) can always be rebuilt with
  `knowledge-engine reindex` and the embeddings `build` command. You do not
  need to back them up — though backing them up saves a rebuild on restore.
- `corpus/registry.json` *is* worth backing up — it records what's enabled.
  It's small and lives with the corpus.

## A note on Docker

A Docker Compose template is **not** part of v1.0. The engine is a standard
Python package, so containerizing it is straightforward if you want to — a
slim Python base image, `pip install -e ./engine`, the uvicorn command as the
entrypoint, and the corpus mounted as a volume. A maintained Compose template
is on the roadmap for a later release.

## Upgrades

Knowledge-Engine is distributed as a repository. To take an update: pull the
new version (or re-download), re-run the install script to pick up any new
dependencies, and run `knowledge-engine reindex`. Your corpus and
`registry.json` are yours and are not overwritten by an engine update.
