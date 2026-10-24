# Dependencies

> What the engine needs at runtime, what the optional features add, and what
> the development tooling pulls in. Versions are pinned in `engine/uv.lock`;
> `uv lock --check` in CI fails when the lockfile drifts from
> `engine/pyproject.toml`. This page lists the locked versions at the 3.0.0
> release; regenerate the numbers from the lockfile when they move.

## Runtime (installed with the package)

Python **3.12 or newer**. Eleven direct dependencies (`[project.dependencies]`
in `engine/pyproject.toml`), 40 packages in total once their own dependencies
are resolved:

| Package | Locked | Purpose |
| --- | --- | --- |
| `fastapi` | 0.161.1 | HTTP API, dashboard mount, OpenAPI document |
| `starlette` | 1.9.0 | ASGI primitives used directly by `core/http.py` and the MCP mount (also pulled in by FastAPI) |
| `uvicorn[standard]` | 0.54.0 | ASGI server behind `knowledge-engine serve` / `board-serve`; `standard` adds `uvloop`, `httptools`, `watchfiles`, `websockets`, `python-dotenv` |
| `pydantic` | 2.14.1 | Request / response models, settings validation |
| `pydantic-settings` | 2.16.1 | `core/settings.py`: typed `KE_*` environment settings |
| `mcp` | 2.3.0 | Official MCP Python SDK: stdio and streamable-HTTP transports, tool registration (`mcp>=2.3,<3`) |
| `anyio` | 4.15.1 | Async runtime glue in `knowledge_engine.mcp` (stdio transport, in-process selftest) |
| `httpx` | 0.28.1 | HTTP client for the Ollama and OpenAI-compatible providers, embedding providers, tool-host proxying and the board CLI |
| `jsonschema` | 4.26.0 | Registry validation against `core/registry.schema.json` (Draft 2020-12) |
| `pyyaml` | 6.0.3 | Pipeline YAML config loader (`runtime/foundation/config.py`) |
| `watchdog` | 6.0.0 | `knowledge-engine watch`: auto-register new corpus folders |

SQLite (with the FTS5 extension) comes from the Python standard library; the
FTS5 indexer logs a warning if the interpreter's SQLite was built without it.

## Optional features (external software, no extra Python packages)

| Feature | Needs | Enabled by |
| --- | --- | --- |
| `ollama` generation provider | An Ollama server at `OLLAMA_BASE_URL` | Always registered; reachability probed lazily and cached (`KE_PROVIDER_PROBE_TTL`); unreachable → next provider |
| `cloud-http` generation provider | An OpenAI-compatible endpoint + key (`KE_CLOUD_ENDPOINT`, `KE_CLOUD_API_KEY`, optional `KE_CLOUD_MODEL`) | Registered first when both endpoint and key are set; otherwise absent |
| Corpus embeddings (`vector` / `hybrid` search) | Ollama with an embedding model (`bge-m3` by default), or a remote endpoint with `KE_EMBED_ALLOW_REMOTE=1`; `fake` needs nothing | `knowledge-engine embeddings build`; see `01-user-docs/EMBEDDINGS.md` |
| Project-context embeddings | Same providers, configured in `knowledge-engine.toml` (`[project_context.embeddings]`) | `project_context.generate_embeddings` MCP tool / CLI |
| Task classifier (`runtime/pipeline/task_classifier.py`) | Ollama at `KE_CLASSIFIER_ENDPOINT` with `KE_CLASSIFIER_MODEL` | Called by your own pipeline code |
| Sandboxed execution | Docker / WSL / subprocess executor of your choice | `runtime/sandbox/` `get_sandbox()` hook — bring your own executor |
| Containers | Docker or compatible | `Dockerfile` / `compose.yaml` at the repository root |

The pipeline foundation (`runtime/foundation/`), the multi-worker pipeline
(`runtime/pipeline/`) and the tool host (`tool_host/`) need no extra packages;
their SQLite databases are created under `KE_DATA_DIR` on first use.

## Development groups (`[dependency-groups]`, PEP 735)

Installed with `uv sync --project engine` (the `dev` group is the default) or
selectively with `--group`. Not installed by end users.

| Group | Packages (locked) | Used by |
| --- | --- | --- |
| `lint` | `ruff` 0.17.2, `mypy` 2.4.0, `types-PyYAML`, `types-jsonschema` | `just lint`, `just typecheck`, the CI `lint` job, pre-commit |
| `test` | `pytest` 9.1.1, `pytest-cov`, `pytest-randomly`, `pytest-repeat`, `pytest-xdist`, `hypothesis`, `httpx2` (Starlette's test client) | `just test`, the CI `test` matrix |
| `build` | `twine` 7.0.0 | Metadata check of the sdist / wheel (`just build`, CI `build`, release) |
| `dev` | `lint` + `test` + `build` + `pre-commit` 4.7.0 | Everyday development |
| `ui` | `playwright` 1.64.1 | Opt-in browser smoke test (`just ui-test`, `KE_UI_TESTS=1`) |

Formatting and linting are done by **ruff** alone (`[tool.ruff]` in
`engine/pyproject.toml`); there is no black configuration. Workflow files are
checked by `zizmor` (pre-commit hook and CI).

Outside Python:

- **Node 22+ / npm** — only to rebuild the dashboard stylesheet from
  `ui-src/` (`just ui-css`); the compiled `tailwind.css` is committed and the
  package never needs Node at runtime.
- **`just`** — optional task runner; every recipe is a plain `uv` command.
- **`lychee`** — the offline link checker run by the CI `docs` job
  (`lycheeverse/lychee-action`); `scripts/check_docs_paths.py` needs only the
  standard library and an importable `knowledge_engine`.

## Keeping them current

- Dependabot opens weekly grouped PRs for GitHub Actions (`ci(deps)`) and for
  the `uv` lockfile (`build(deps)`), after a 7-day cooldown.
- After editing `engine/pyproject.toml`: `uv lock --project engine` and commit
  `uv.lock` (`just lock`).
- Vendored dashboard assets (Alpine.js) are pinned by URL and SHA-384 in
  `scripts/vendor.py`; `python scripts/vendor.py --check` verifies them offline.

## Skill-level dependencies

Skills you add to `corpus/skills/` can declare external dependencies in the
registry entry's `external_deps` array (free text, e.g. `"Tavily API key"`,
`"ripgrep on PATH"`). The engine does not enforce them — they are surfaced in
the dashboard's skill detail so users see at a glance what the skill needs at
runtime. The bundled `corpus/samples/demo-skill/` has none.
