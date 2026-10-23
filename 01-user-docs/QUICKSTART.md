# Quickstart

Goal: from a fresh copy of the repo to a working dashboard with searchable
content, in about ten minutes. Every command below has been run on a clean
checkout.

## Prerequisites

- **Python 3.10 or newer.** Check with `python --version`.
- About 200 MB of disk for the virtualenv and dependencies.
- That's it. No Node toolchain, no database server, no Docker.

## 1. Install (≈ 3 minutes)

Install [`uv`](https://docs.astral.sh/uv/), then create the project
virtualenv under `engine/.venv` (the engine is installed into it editable):

```bash
uv sync --project engine
```

Every command below is run through `uv run --project engine ...`; to type
`knowledge-engine` directly instead, activate the venv
(`source engine/.venv/bin/activate`, or `.\engine\.venv\Scripts\Activate.ps1`
on Windows).

## 2. Bootstrap (≈ 5 seconds)

`bootstrap` scans the conventional corpus folders (`corpus/libraries/`,
`skills/`, `kits/`, `capabilities/`) and registers any new folder it finds into
`corpus/registry.json`.

```
knowledge-engine bootstrap
```

The demo library and demo skill ship **already registered** in `registry.json`,
and those convention folders don't exist on a fresh clone, so `bootstrap` has
nothing new to do:

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

Two enabled entries (the demo library + the demo skill), 6 markdown files
indexed.

## 4. Confirm from the CLI (≈ 10 seconds)

Before opening the dashboard, prove search works:

```
knowledge-engine search "demo"
```

You should get a JSON array of ranked hits with highlighted snippets from the
demo library. If you see results, the engine is working.

`knowledge-engine info` prints the resolved paths and corpus counts — useful if
anything looks off.

## 5. Serve the dashboard (≈ 5 seconds)

```bash
uv run --project engine knowledge-engine serve
```

The server starts on port **9210**. Open:

- **Dashboard:** <http://127.0.0.1:9210/ui/>
- **API docs (Swagger):** <http://127.0.0.1:9210/docs>

In the dashboard you can type a query, see ranked results with snippets, filter
by kind, toggle libraries on and off, and trigger a reindex — all without
touching the CLI again.

> `serve` uses port 9210. To change it, run `knowledge-engine serve --port <n>`.

## You are at the 10-minute milestone

At this point you have: a running dashboard, 6 indexed files, working search,
and an API. The demo content is there only to prove the pipeline — the next
step is your own content.

## What's next

- **Add your own content** → [BRING-YOUR-OWN-CORPUS.md](BRING-YOUR-OWN-CORPUS.md)
- **Wire it into Claude / Cursor / Continue** → [MCP-WIRING.md](MCP-WIRING.md)
- **Turn on semantic search** → [EMBEDDINGS.md](EMBEDDINGS.md)

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `python` not found | Install Python 3.12 or newer and ensure it is on PATH (or let `uv` fetch one: `uv python install 3.13`). |
| `knowledge-engine` not found | Run it through uv: `uv run --project engine knowledge-engine …`, or activate `engine/.venv` first. |
| `reindex` reports `files: 0` | Nothing is enabled. Run `bootstrap`, then check `knowledge-engine info`. |
| Dashboard loads but is blank | The dashboard's assets ship inside the package (no CDN). Check the server log for `dashboard mounted at /ui`; if it is missing, reinstall with `uv sync --project engine`. |
| Port 9210 already in use | Pass `--port <n>` to `knowledge-engine serve`, or set `KE_PORT` to a free port. |
| `uvicorn not installed` | Re-sync the environment: `uv sync --project engine`. |
