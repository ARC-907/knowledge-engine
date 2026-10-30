# Knowledge-Engine — Documentation

Guides for getting real value out of Knowledge-Engine. Start at the top and
work down; each guide is self-contained.

| Guide | Read it when you want to… |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | Install, run, and see search working in about ten minutes. |
| [UPGRADING.md](UPGRADING.md) | Upgrade from 3.0 to 3.1, turn on the new features, roll back. |
| [BRING-YOUR-OWN-CORPUS.md](BRING-YOUR-OWN-CORPUS.md) | Replace the demo content with your own. |
| [LIBRARY-AUTHORING.md](LIBRARY-AUTHORING.md) | Author a domain library with the three-lens framework. |
| [SKILL-AUTHORING.md](SKILL-AUTHORING.md) | Add skill packages the engine can index and surface. |
| [MCP-WIRING.md](MCP-WIRING.md) | Connect the MCP server to Claude Desktop, Cursor, Continue or any other MCP client. |
| [EMBEDDINGS.md](EMBEDDINGS.md) | Turn on semantic (`vector`) and `hybrid` search alongside FTS5. |
| [SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md) | Organise content into workspaces, projects and collections; migrate the corpus metadata. |
| [KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md) | Scoped search and open across the corpus and linked project context (the retrieval broker). |
| [CITATIONS.md](CITATIONS.md) | Record corpus revisions, open exact evidence, validate claims and export citations. |
| [MODULES.md](MODULES.md) | Register, configure and run executable utilities (modules) through CLI, HTTP and MCP; author your own. |
| [SERENA.md](SERENA.md) | Optional, read-only code-symbol lookup in your repositories through a separately installed Serena. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Run the engine beyond your laptop — service, LAN, Docker, hardening, backups, upgrades. |
| [CONFIGURATION.md](CONFIGURATION.md) | Look up every environment variable, its type and default (generated from the code). |
| [DASHBOARD.md](DASHBOARD.md) | Use the browser dashboard: search, scoped knowledge, citations, module status. |
| [THEMING.md](THEMING.md) | Restyle or rebuild the dashboard. |
| [FAQ.md](FAQ.md) | Quick answers to the common questions. |

Engine internals (architecture, module catalog, agent board, dependencies,
decision records) live in [`02-dev-docs/`](../02-dev-docs/ARCHITECTURE.md);
the version history is in [`CHANGELOG.md`](../CHANGELOG.md).

## The 30-second model

- **Corpus** — your markdown, in `corpus/` (or wherever `KE_CORPUS_ROOT`
  points). This is data; you own it. `corpus/registry.json` lists what is
  enabled.
- **Engine** — the Python package in `engine/`. Indexes the corpus, serves an
  HTTP API, runs an MCP server. MIT-licensed; fork it freely.
- **Dashboard** — `engine/src/knowledge_engine/server/static/`, served at
  `/ui/`. A thin client over the engine's HTTP API.

You bring the corpus. The engine makes it searchable — in a browser, from the
CLI, and from any MCP-capable AI assistant.

## The happy path

```
uv sync  →  bootstrap  →  reindex  →  serve  →  search
```

`uv sync --project engine` creates the virtualenv and installs the engine.
`bootstrap` registers every library / skill folder it finds. `reindex` builds
the full-text index. `serve` starts the dashboard and API. Then you search —
in the browser, on the CLI (`knowledge-engine search "query"`), or through MCP.

See [QUICKSTART.md](QUICKSTART.md) for the step-by-step.
