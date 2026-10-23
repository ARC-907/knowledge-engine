# Knowledge-Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)
[![Version 3.0.0](https://img.shields.io/badge/version-3.0.0-blue.svg)](CHANGELOG.md)
[![MCP protocol](https://img.shields.io/badge/MCP-2026--07--28-7C3AED.svg)](https://modelcontextprotocol.io)
![FTS5 + bge-m3](https://img.shields.io/badge/search-FTS5%20%2B%20bge--m3-003B57.svg?logo=sqlite&logoColor=white)

> Self-hosted knowledge engine for a folder of markdown. Search it in a browser,
> from the CLI, and from any MCP client. Everything runs locally; MIT-licensed.

Knowledge-Engine indexes a corpus of markdown, serves full-text (and optional
semantic / hybrid) search over an HTTP API and a local dashboard, and exposes
the same search to AI assistants through an MCP server (stdio or streamable
HTTP). You bring the corpus; the engine makes it searchable.

## Quickstart

Prerequisite: [`uv`](https://docs.astral.sh/uv/). It installs a suitable Python
(3.12 or newer) for you. No Node toolchain is needed to run the engine or the
dashboard.

```bash
# Windows, macOS, Linux — same commands, from the repository root
uv sync --project engine                           # engine/.venv with the engine + dev tools
uv run --project engine knowledge-engine bootstrap # register corpus folders (demo corpus: nothing new)
uv run --project engine knowledge-engine reindex   # build the FTS5 index -> {"entries": 2, "files": 6, ...}
uv run --project engine knowledge-engine serve     # http://127.0.0.1:9210/ui/
```

After `serve`, the dashboard is at <http://127.0.0.1:9210/ui/> and the OpenAPI
docs at <http://127.0.0.1:9210/docs>. `knowledge-engine serve --port <n>` (or
`KE_PORT`) changes the port; `knowledge-engine capabilities` prints the
retrieval and capability surfaces of the installation (including substrates
that are available but empty).

Prefer a global `knowledge-engine` command instead of a checkout? Releases are
published on GitHub (the package is not on PyPI, where the name belongs to an
unrelated project):

```bash
uv tool install "git+https://github.com/ARC-907/knowledge-engine@v3.0.0#subdirectory=engine"
cd /path/to/your/checkout-or-corpus-root   # the directory containing corpus/registry.json, or set KE_HOME
knowledge-engine serve
```

`uv tool install` also accepts the wheel attached to a
[GitHub Release](https://github.com/ARC-907/knowledge-engine/releases). The
installed command resolves its paths from `KE_HOME` (default: the nearest
directory at or above the working directory that contains
`corpus/registry.json`), or from `KE_CORPUS_ROOT` / `KE_DATA_DIR` /
`KE_REGISTRY_PATH` individually — see
[CONFIGURATION.md](01-user-docs/CONFIGURATION.md).

The repository ships a small demo corpus — one library
(`corpus/samples/demo-library`) and one skill (`corpus/samples/demo-skill`),
both enabled in `corpus/registry.json` — so `reindex` has real content on a
fresh clone. Replace it with your own content: drop folders into
`corpus/libraries/`, `corpus/skills/`, … and run `bootstrap` + `reindex`, or
point `KE_CORPUS_ROOT` at an existing markdown tree. See
[BRING-YOUR-OWN-CORPUS.md](01-user-docs/BRING-YOUR-OWN-CORPUS.md) and
[LIBRARY-AUTHORING.md](01-user-docs/LIBRARY-AUTHORING.md). If `registry.json`
references a library that is not present, the indexer skips it and logs a
warning naming the missing entry.

## What's in the box

- **HTTP API + dashboard** — FastAPI app with health, info, search, registry,
  generation and agent-board routes; Alpine.js dashboard at `/ui/` (compiled
  Tailwind, light/dark, keyboard-accessible, no CDN).
- **MCP server** on the official MCP Python SDK — stdio or streamable HTTP,
  protocol `2026-07-28` (clients using `initialize` negotiate back to
  `2024-11-05`). Works with any MCP client.
- **Search** — SQLite FTS5 with `bm25` ranking and snippet highlighting;
  optional embedding index (bge-m3 via Ollama by default) for `vector` and
  `hybrid` search modes.
- **Registry** — `corpus/registry.json`, validated against a JSON Schema on
  every load and write; `knowledge-engine registry validate`.
- **Generation providers** — echo / Ollama / OpenAI-compatible HTTP, selected
  per request or by `KE_PROVIDER_ORDER`.
- **Substrates** you populate with your own projects and agents: project
  context (code, docs, logs and git history per project), the agent board
  (SQLite message bus with scopes, keys and a sweeper), hosted tools and a
  sandbox hook.
- **Typed configuration** — every environment variable is declared in one
  place; `knowledge-engine config show` prints the effective values with
  secrets masked.

## MCP server

```bash
knowledge-engine mcp              # stdio; protocol 2026-07-28 (initialize clients back to 2024-11-05)
knowledge-engine mcp --http       # streamable HTTP at http://127.0.0.1:9211/mcp
knowledge-engine mcp --selftest   # build the server, list its tools, exit 0
KE_MCP_HTTP=1 knowledge-engine serve   # the same server mounted at /mcp on the API server
```

The base tools are `search`, `registry_list`, `registry_toggle` and
`registry_get`. The project-context (46 tools) and agent-board (15 tools)
groups are mounted when their packages import and are enabled in config; a
default install lists 65 tools. Tool failures come back as `isError` tool
results. See [MCP-WIRING.md](01-user-docs/MCP-WIRING.md) for client setup and
[scripts/mcp-client-config.example.json](scripts/mcp-client-config.example.json)
for a ready-to-edit config.

## Command line

`knowledge-engine --help` lists every command. The global flags `-v` / `-vv`
raise the stderr log level to INFO / DEBUG and `-q` / `-qq` lower it to ERROR /
CRITICAL. One-shot commands log at WARNING by default; the long-running
commands (`serve`, `board-serve`, `mcp`, `watch`) default to `KE_LOG_LEVEL`
(INFO), so the startup policy lines (`CORS: allow_origins=...`,
`board trust: ...`, `dashboard mounted at /ui`) are visible without extra flags.

## Documentation

- **User guides** — [`01-user-docs/`](01-user-docs/README.md): Quickstart,
  bring your own corpus, library and skill authoring, MCP wiring, embeddings,
  deployment (including Docker), configuration reference, theming, FAQ.
- **Engine internals** — [ARCHITECTURE.md](02-dev-docs/ARCHITECTURE.md),
  [CATALOG.md](02-dev-docs/CATALOG.md), [AGENT-BOARD.md](02-dev-docs/AGENT-BOARD.md),
  [DEPENDENCIES.md](02-dev-docs/DEPENDENCIES.md), [GLOSSARY.md](02-dev-docs/GLOSSARY.md),
  decision records under [`02-dev-docs/adr/`](02-dev-docs/adr/).
- **Project** — [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md),
  [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
  [RELEASING.md](02-dev-docs/RELEASING.md).

## What this does NOT give you

- A pre-built knowledge base — you supply the libraries.
- Authentication for the core routes — `/search`, `/registry` and `/generate`
  are local-trust (loopback bind, loopback CORS); put a reverse proxy with
  authentication in front before exposing them. The agent board's admin routes
  do require a key. See [DEPLOYMENT.md](01-user-docs/DEPLOYMENT.md) and
  [SECURITY.md](SECURITY.md).
- A preconfigured multi-agent runtime — the engine ships the knowledge, board,
  tool-hosting and sandbox substrates; you provide the agents, tools, skills
  and execution adapter.

## License

MIT — see [`LICENSE`](LICENSE). Fork, modify and redistribute (including
commercially) without restriction.
