# Knowledge-Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)
[![Status: v2.0](https://img.shields.io/badge/status-v2.0-blue.svg)](02-dev-docs/CHANGELOG.md)
[![MCP protocol](https://img.shields.io/badge/MCP-2026--07--28-7C3AED.svg)](https://modelcontextprotocol.io)
![FTS5 + bge-m3](https://img.shields.io/badge/search-FTS5%20%2B%20bge--m3-003B57.svg?logo=sqlite&logoColor=white)

> Self-hosted knowledge engine for your markdown notes. Search them from Claude Desktop, Cursor, or any MCP client. Run the dashboard locally, point it at your own corpus.

Knowledge-Engine indexes a folder of markdown, serves full-text (and optional semantic) search over an HTTP API and a local dashboard, and exposes that same search to AI assistants through an MCP stdio server. You bring the corpus; the engine makes it searchable — in a browser, from the CLI, and from any MCP-capable assistant. Everything runs locally and is MIT-licensed.

## Quickstart

> **Prerequisite (Debian / Ubuntu):** install `python3-venv` first — `sudo apt install python3-venv`. Required by `python3 -m venv`; bundled on macOS and Windows.

```pwsh
# Windows
.\scripts\install.ps1
.\engine\.venv\Scripts\Activate.ps1
knowledge-engine bootstrap
knowledge-engine capabilities
knowledge-engine reindex
.\scripts\serve.ps1
# open http://127.0.0.1:9210/ui/
```

```bash
# macOS / Linux
./scripts/install.sh
source engine/.venv/bin/activate
knowledge-engine bootstrap
knowledge-engine capabilities
knowledge-engine reindex
./scripts/serve.sh
# open http://127.0.0.1:9210/ui/
```

After `serve`, the dashboard is at <http://127.0.0.1:9210/ui/> and the OpenAPI docs are at <http://127.0.0.1:9210/docs>.

The repo ships a small demo corpus — **1 demo library** (`corpus/samples/demo-library`) and **1 demo skill** (`corpus/samples/demo-skill`), both enabled by default — so `reindex` does real work on a fresh clone. Replace them with your own content when you're ready (see [Bring your own corpus](01-user-docs/BRING-YOUR-OWN-CORPUS.md)).

If `corpus/registry.json` references a library that isn't present in `corpus/`, the indexer skips it and `knowledge-engine bootstrap` prints a warning naming the missing library.

**To bring your own corpus:** drop folders into `corpus/libraries/` (gitignored by default) or anywhere you point the registry at. Authoring guidance lives in [BRING-YOUR-OWN-CORPUS.md](01-user-docs/BRING-YOUR-OWN-CORPUS.md) and [LIBRARY-AUTHORING.md](01-user-docs/LIBRARY-AUTHORING.md).

## What's in the box

- FastAPI app with health, info, search, registry, generation, and board routes; single-file Alpine.js + Tailwind dashboard at `/ui/`
- MCP server on the official MCP Python SDK (stdio or streamable HTTP; protocol `2026-07-28`, `initialize` clients back to `2024-11-05`) — compatible with Claude Desktop, Cursor, Continue, any MCP client
- SQLite FTS5 indexer with `bm25` ranking + snippet highlighting; optional bge-m3 embedding index for semantic search
- Pluggable provider abstraction (echo / Ollama / cloud HTTP stub) for generation routing
- Project-context, agent-board, hosted-tool, and sandbox substrates that you can populate with your own projects and agents
- Demo corpus under `corpus/samples/`, registry definitions at `corpus/registry.json` + `corpus/registry.schema.json`

## MCP server

```pwsh
knowledge-engine mcp              # stdio; protocol 2026-07-28 (initialize clients back to 2024-11-05)
knowledge-engine mcp --http       # streamable HTTP at http://127.0.0.1:9211/mcp
knowledge-engine mcp --selftest   # build the server, list its tools, exit 0
```

The base tools are `search`, `registry_list`, `registry_toggle`, and
`registry_get`. Optional tool groups add project-context and board tools when the
runtime can import them and they're enabled in config. Run
`knowledge-engine capabilities` to see the current retrieval and capability
surfaces, including empty-but-available substrates. See
[scripts/mcp-client-config.example.json](scripts/mcp-client-config.example.json)
for a ready-to-edit config, and [MCP-WIRING.md](01-user-docs/MCP-WIRING.md) for
client setup.

## Documentation

- **User guides** — [`01-user-docs/`](01-user-docs/README.md): install (QUICKSTART), bring-your-own-corpus, library + skill authoring, MCP wiring, embeddings, deployment, theming, FAQ.
- **Engine internals** — [ARCHITECTURE.md](02-dev-docs/ARCHITECTURE.md), [CATALOG.md](02-dev-docs/CATALOG.md), and [CHANGELOG.md](02-dev-docs/CHANGELOG.md).

## What this does NOT give you

- A pre-built knowledge base — you supply the libraries.
- Authentication / multi-user separation out of the box — the default app is local-trust; harden before exposing (see [DEPLOYMENT.md](01-user-docs/DEPLOYMENT.md)).
- A preconfigured multi-worker agent runtime — the engine ships the knowledge,
  board, tool-hosting, and sandbox-adapter substrates, but you provide the
  project-specific agents, tools, skills, and execution adapter.

## License

MIT — see [`LICENSE`](LICENSE). Fork, modify, and redistribute (including commercially) without restriction.

Semver applies to the engine package, MCP protocol, and registry schema.
