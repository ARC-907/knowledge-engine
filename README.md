# Knowledge-Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)
[![Status: v1.2](https://img.shields.io/badge/status-v1.2-blue.svg)](CHANGELOG.md)
[![MCP protocol](https://img.shields.io/badge/MCP-2024--11--05-7C3AED.svg)](https://modelcontextprotocol.io)
[![FTS5 + bge-m3](https://img.shields.io/badge/search-FTS5%20%2B%20bge--m3-003B57.svg?logo=sqlite&logoColor=white)]()

> Self-hosted knowledge engine for your markdown notes. Search them from Claude Desktop, Cursor, or any MCP client. Run the dashboard locally, point it at your own corpus.

This is the **free MIT-licensed engine + dashboard + demo content**. The paid Standard bundle — which adds 3 curated reference libraries, 9 buyer guides, JSON schemas + prompt templates — is delivered as a downloadable ZIP via Polar: [buy.polar.sh/fb5e8614-3965-4e8a-86db-59846c11143e](https://buy.polar.sh/fb5e8614-3965-4e8a-86db-59846c11143e) ($199 one-time, 30-day refund).

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

**Public starter ships 1 demo library** (`samples/demo-library`, enabled by default) plus 1 demo skill — enough for `reindex` to do real work on a fresh clone. The Standard bundle adds **3 curated reference libraries** (Decision Analysis, AI Monetization, System Design) plus 9 buyer guides — see [buy.polar.sh/fb5e8614-3965-4e8a-86db-59846c11143e](https://buy.polar.sh/fb5e8614-3965-4e8a-86db-59846c11143e).

If `corpus/registry.json` references a library that isn't present in `corpus/` (for example, you've copied a registry from the Pro bundle into a free repo), the indexer skips it and `knowledge-engine bootstrap` will print a warning naming the missing library.

**To bring your own corpus:** drop folders into `corpus/libraries/` (which is gitignored / buyer-tier in the public repo), or anywhere you point the registry at. Authoring guidance lives in the Standard bundle's `docs/BRING-YOUR-OWN-CORPUS.md` and `docs/LIBRARY-AUTHORING.md`.

## What ships in this free MIT repo

- FastAPI app with search, registry, generation, and board routes; single-file Alpine.js + Tailwind dashboard at `/ui/`
- MCP stdio server (JSON-RPC 2.0, protocol `2024-11-05`) — compatible with Claude Desktop, Cursor, Continue, any MCP client
- SQLite FTS5 indexer with `bm25` ranking + snippet highlighting; optional bge-m3 embedding index for semantic search
- Pluggable provider abstraction (echo / Ollama / cloud HTTP stub) for generation routing
- Project-docs, board, hosted-tool, and sandbox substrates that can be populated by your projects and agents
- Demo corpus under `corpus/samples/`, registry definitions at `corpus/registry.json` + `corpus/registry.schema.json`

## What the paid Standard bundle adds

Delivered as a ZIP via Polar at purchase:

- **3 reference libraries** (~42 curated markdown files) applying the three-lens framework methodology (FRAME→ANALYZE→DECIDE, IDENTIFY→EVALUATE→EXECUTE, FRAME→DESIGN→EVOLVE) — demonstration material you can study or fork
- **9 buyer guides** (`QUICKSTART`, `BRING-YOUR-OWN-CORPUS`, `LIBRARY-AUTHORING`, `SKILL-AUTHORING`, `MCP-WIRING`, `EMBEDDINGS`, `DEPLOYMENT`, `THEMING`, `FAQ`)
- **10 JSON schemas** + **12 prompt templates** for structured corpus authoring
- Single-buyer commercial-permissive license — use the material in your own products and paid client work; no per-seat fee; don't resell the bundle itself

[**buy.polar.sh/fb5e8614-3965-4e8a-86db-59846c11143e**](https://buy.polar.sh/fb5e8614-3965-4e8a-86db-59846c11143e) — $199 one-time, 30-day no-questions refund.

## MCP server

```pwsh
knowledge-engine mcp   # JSON-RPC 2.0 over stdio, protocol 2024-11-05
```

The base tools are `search`, `registry_list`, `registry_toggle`, and
`registry_get`. Optional tool groups add project-docs and board tools when the
runtime can import them. Run `knowledge-engine capabilities` to see the current
retrieval and capability surfaces, including empty-but-available substrates.
See [scripts/mcp-client-config.example.json](scripts/mcp-client-config.example.json)
for a ready-to-edit config.

## Documentation

[ARCHITECTURE.md](ARCHITECTURE.md), [CATALOG.md](CATALOG.md), and [CHANGELOG.md](CHANGELOG.md) cover the engine layout. The detailed buyer guides (QUICKSTART, BYO-corpus, library authoring, deployment, etc.) ship with the paid Standard bundle.

## What this does NOT give you

- A pre-built knowledge base — you supply the libraries (or buy the Standard bundle for 3 curated ones)
- Authentication / multi-user separation out of the box — the default app is local-trust; harden before exposing
- A preconfigured multi-worker agent runtime — the engine ships the knowledge,
  board, tool-hosting, and sandbox-adapter substrates, but you provide the
  project-specific agents, tools, skills, and execution adapter.

## License

- This repo (engine + dashboard + demo corpus) is **MIT** — [`LICENSE`](LICENSE). Fork, modify, redistribute commercially without restriction.
- The Standard bundle (delivered via Polar) is **Single-Buyer Commercial-Permissive**. Full terms in the bundle's `LICENSE-BUYER.md`.

Where a file is covered by both licenses, the MIT grant controls — it is strictly more permissive.

Semver applies to the engine package, MCP protocol, and registry schema.

## Refunds

30 days, no questions, automatic via the storefront. If the Standard bundle doesn't fit your situation, refund — don't sit on it.
