# Quickstart

Goal: from a fresh copy of the repository to a working dashboard with
searchable content, in about ten minutes. Every command below has been run on a
clean checkout.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/). It installs a suitable Python (3.12 or
  newer) if your machine has none.
- About 200 MB of disk for the virtualenv and dependencies.
- That's it. No Node toolchain, no database server, no Docker.

## 1. Install (≈ 1 minute)

Create the project virtualenv under `engine/.venv` with the engine installed
editable and the development tools alongside:

```bash
uv sync --project engine
```

Every command below is run through `uv run --project engine …`. To type
`knowledge-engine` directly instead, activate the venv
(`source engine/.venv/bin/activate`, or `.\engine\.venv\Scripts\Activate.ps1`
on Windows) — the rest of this guide assumes you did.

The engine finds the corpus from `KE_HOME`, which defaults to the nearest
directory at or above your working directory that contains
`corpus/registry.json` — so run the commands from anywhere inside the
checkout. Runtime data (indexes, databases) goes to
`<KE_HOME>/.knowledge-engine/data`, which is gitignored.

## 2. Bootstrap (≈ 5 seconds)

`bootstrap` scans the conventional corpus folders (`corpus/libraries/`,
`skills/`, `kits/`, `capabilities/`) and registers any new folder it finds in
`corpus/registry.json`.

```
knowledge-engine bootstrap
```

The demo library and demo skill ship **already registered**, and the
convention folders don't exist on a fresh clone, so `bootstrap` has nothing new
to do:

```json
{ "library": 0, "skill": 0, "kit": 0, "tool": 0, "skipped": 0 }
```

That is correct. When you later add your own folders under `corpus/`,
`bootstrap` picks them up.

## 3. Reindex (≈ 5 seconds)

`reindex` builds the SQLite FTS5 full-text index over every **enabled** entry.

```
knowledge-engine reindex
```

Expected output on the shipped demo corpus:

```json
{ "entries": 2, "files": 6, "missing": 0 }
```

Two enabled entries (the demo library and the demo skill), six markdown files
indexed.

## 4. Confirm from the CLI (≈ 10 seconds)

Before opening the dashboard, prove search works:

```
knowledge-engine search "demo"
```

You get a JSON array of ranked hits (`entry_id`, `entry_kind`, `path`,
`snippet` with `<mark>` highlights, `score`) from the demo library.

`knowledge-engine info` prints the resolved paths and corpus counts — the
first thing to check if anything looks off. `knowledge-engine config show`
prints every setting's effective value.

## 5. Serve the dashboard (≈ 5 seconds)

```bash
knowledge-engine serve
```

The server binds `127.0.0.1:9210`. Open:

- **Dashboard:** <http://127.0.0.1:9210/ui/>
- **API docs (Swagger):** <http://127.0.0.1:9210/docs>

In the dashboard you can type a query, see ranked results, filter by kind,
toggle libraries on and off, and trigger a reindex — all without touching the
CLI again. The Board and Config tabs expose the agent board; its admin actions
need an API key (see [`02-dev-docs/AGENT-BOARD.md`](../02-dev-docs/AGENT-BOARD.md)).
The Knowledge and Modules tabs cover scoped search, citations and module
status; [DASHBOARD.md](DASHBOARD.md) describes every tab.

> `serve` uses port 9210. To change it, run `knowledge-engine serve --port <n>`
> or set `KE_PORT`.

## You are at the 10-minute milestone

You now have a running dashboard, six indexed files, working search, and an
API. The demo content only proves the pipeline — the next step is your own
content.

## What's next

- **Add your own content** → [BRING-YOUR-OWN-CORPUS.md](BRING-YOUR-OWN-CORPUS.md)
- **Wire it into an MCP client** → [MCP-WIRING.md](MCP-WIRING.md)
- **Turn on semantic search** → [EMBEDDINGS.md](EMBEDDINGS.md)
- **Run it somewhere other than your laptop** → [DEPLOYMENT.md](DEPLOYMENT.md)

## Logging

Commands log to stderr. One-shot commands (`bootstrap`, `reindex`, `search`,
…) show WARNING and above; add `-v` for INFO (`-vv` for DEBUG) or `-q` / `-qq`
to show only ERROR / CRITICAL:

```
knowledge-engine -v reindex
```

The long-running commands (`serve`, `board-serve`, `mcp`, `watch`) default to
`KE_LOG_LEVEL` (INFO), so their startup lines — `dashboard mounted at /ui`,
`CORS: allow_origins=...`, `board trust: ...` — are visible without flags.
`-v` / `-q` override `KE_LOG_LEVEL` for one run.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `uv` not found | Install it from <https://docs.astral.sh/uv/>; it fetches Python 3.12+ on its own (`uv python install 3.13`). |
| `knowledge-engine` not found | Run it through uv: `uv run --project engine knowledge-engine …`, or activate `engine/.venv` first. |
| `reindex` reports `files: 0` | Nothing is enabled. Run `bootstrap`, then check `knowledge-engine info`. |
| `reindex` warns about a missing library | `registry.json` names a folder that is not in the corpus. Add the folder or disable the entry. |
| Engine refuses to start: registry invalid | `knowledge-engine registry validate` lists every problem with its JSON pointer. |
| Dashboard loads but is blank | The dashboard ships inside the package (no CDN). Look for `dashboard mounted at /ui` in the server log; if it is missing, reinstall with `uv sync --project engine`. |
| Port 9210 already in use | Pass `--port <n>` to `knowledge-engine serve`, or set `KE_PORT`. |
| `uvicorn not installed` | Re-sync the environment: `uv sync --project engine`. |
