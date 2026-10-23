# Dependencies

> Concrete dependency map for v1.0. Required-core deps install via `scripts/install.{ps1,sh}`; optional features are loud-opt-in.

## Required-core (lean install)

- Python 3.10+
- `fastapi`, `uvicorn` — dashboard / API surface
- `httpx` — HTTP client (cloud routing, future integrations)
- `pydantic-settings` — typed `KE_*` environment settings (`core/settings.py`)
- `watchdog` — file-watch for re-index (soft-required; falls back to manual re-bootstrap if missing)
- `pytest` — included so smoke tests are runnable post-install

All required-core deps are pinned in `engine/pyproject.toml`.

## Optional-feature (loud-opt-in)

| Feature | Deps | Enabled by |
| --- | --- | --- |
| Local model routing via Ollama | Ollama installed at `OLLAMA_BASE_URL` | always registered; reachability probed lazily and cached (`KE_PROVIDER_PROBE_TTL`), unreachable → next provider |
| Sandboxed agent execution | Docker / WSL / subprocess executor of your choice | `runtime/sandbox/` `get_sandbox()` hook — bring your own executor |
| Cloud model routing | An OpenAI-compatible endpoint + key (`KE_CLOUD_ENDPOINT`, `KE_CLOUD_API_KEY`, `KE_CLOUD_MODEL`) | `cloud-http` provider registered first when both endpoint and key are set; otherwise not registered |
| Filename / fulltext index over external files | Index those files into FTS5 by registering their parent folder | manual — `knowledge-engine reindex` |
| Pipeline foundation (`runtime/foundation/`) | `pyyaml` for config-loader YAML files | import-time: `KE_PIPELINE_ROOT` / `KE_PIPELINE_DB` env vars; foundation SQLite auto-creates on first connect |
| Multi-worker pipeline (`runtime/pipeline/`) | `runtime/foundation/` enabled | import-time: enable `runtime/foundation/`, then drive `queue` / `message_board` / `worker_registry` / `task_classifier` from your own worker scripts |
| Tool host (`tool_host/`) | `runtime/foundation/` enabled | import-time: enable `runtime/foundation/`, then register `script` / `service` / `static` tools via the HTTP host |

## Dev-only

- `ruff`, `black` — formatting (configured in `engine/pyproject.toml`)
- `pytest` — already in required-core for smoke tests

## Skill-level dependencies

Skills you add to `corpus/skills/` can declare external deps in the registry entry's `external_deps` array (free-text, e.g. `"Tavily API key"`, `"browser-automation MCP"`). The engine does not enforce these — they are surfaced in the dashboard's skill detail panel so users see at a glance what the skill needs at runtime.

The bundled `corpus/samples/demo-skill/` has no external deps.
