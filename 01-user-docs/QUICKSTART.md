# Quickstart

Goal: from a fresh copy of the repo to a working dashboard with searchable
content, in about ten minutes. Every command below has been run on a clean
checkout.

## Prerequisites

- **Python 3.10 or newer.** Check with `python --version`.
- About 200 MB of disk for the virtualenv and dependencies.
- That's it. No Node toolchain, no database server, no Docker.

## 1. Install (≈ 3 minutes)

The install script creates a virtualenv under `engine/.venv` and installs the
engine package into it as an editable install.

**Windows (PowerShell):**

```pwsh
.\scripts\install.ps1
.\engine\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**

```bash
./scripts/install.sh
source engine/.venv/bin/activate
```

When the script finishes you have a `knowledge-engine` command on your PATH
(inside the activated virtualenv).

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

```pwsh
# Windows
.\scripts\serve.ps1
```

```bash
# macOS / Linux
./scripts/serve.sh
```

The server starts on port **9210**. Open:

- **Dashboard:** <http://127.0.0.1:9210/ui/>
- **API docs (Swagger):** <http://127.0.0.1:9210/docs>

In the dashboard you can type a query, see ranked results with snippets, filter
by kind, toggle libraries on and off, and trigger a reindex — all without
touching the CLI again.

> The serve script uses port 9210. To change it, set `KE_PORT` before running
> the script, or run `knowledge-engine serve --port <n>` directly.

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
| `python` not found | Install Python 3.10+ and ensure it is on PATH. On Windows the `py` launcher also works. |
| `knowledge-engine` not found | Activate the virtualenv first (step 1). The command only exists inside it. |
| `reindex` reports `files: 0` | Nothing is enabled. Run `bootstrap`, then check `knowledge-engine info`. |
| Dashboard loads but is blank | The dashboard pulls Alpine.js + Tailwind from a CDN — it needs internet on first load. For an offline dashboard see [THEMING.md](THEMING.md). |
| Port 9210 already in use | Set `KE_PORT` to a free port before running the serve script. |
| `uvicorn not installed` | Re-run the install script; it installs uvicorn into the virtualenv. |
