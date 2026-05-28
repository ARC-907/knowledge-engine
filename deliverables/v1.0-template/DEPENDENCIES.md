# Dependencies

> Concrete dependency map for v1.0. Required-core deps install via `scripts/install.{ps1,sh}`; optional features are loud-opt-in.

## Required-core (lean install)

- Python 3.10+
- `fastapi`, `uvicorn` — dashboard / API surface
- `httpx` — HTTP client (cloud routing, future integrations)
- `watchdog` — file-watch for re-index (soft-required; falls back to manual re-bootstrap if missing)
- `pytest` — included so smoke tests are runnable post-install

All required-core deps are pinned in `engine/pyproject.toml`.

## Optional-feature (loud-opt-in)

| Feature | Deps | Enabled by |
| --- | --- | --- |
| Local model routing via Ollama | Ollama installed at `OLLAMA_BASE_URL` | runtime detection; provider registered if reachable, otherwise skipped |
| Sandboxed agent execution | Docker / WSL / subprocess executor of your choice | `engine/sandbox_adapter/` is a scaffold — bring your own executor |
| Cloud model routing | API keys for your provider (e.g. `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`) | runtime detection; cloud provider in read-only mode if keys absent |
| Filename / fulltext index over external files | Index those files into FTS5 by registering their parent folder | manual — `knowledge-engine reindex` |
| Pipeline foundation (`foundation/`) | `pyyaml` for config-loader YAML files | import-time: `KE_PIPELINE_ROOT` / `KE_PIPELINE_DB` env vars; foundation SQLite auto-creates on first connect |
| Multi-worker pipeline (`pipeline/`) | `foundation/` enabled | import-time: enable `foundation/`, then drive `queue` / `message_board` / `worker_registry` / `task_classifier` from your own worker scripts |
| Tool host (`tools/`) | `foundation/` enabled | import-time: enable `foundation/`, then register `script` / `service` / `static` tools via the HTTP host |

## Dev-only

- `ruff`, `black` — formatting (configured in `engine/pyproject.toml`)
- `pytest` — already in required-core for smoke tests

## Skill-level dependencies

Skills you add to `corpus/skills/` can declare external deps in the registry entry's `external_deps` array (free-text, e.g. `"Tavily API key"`, `"browser-automation MCP"`). The engine does not enforce these — they are surfaced in the dashboard's skill detail panel so users see at a glance what the skill needs at runtime.

The bundled `corpus/samples/demo-skill/` has no external deps.
