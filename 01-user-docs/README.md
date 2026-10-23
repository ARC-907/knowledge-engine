# Knowledge-Engine — Documentation

Guides for getting real value out of Knowledge-Engine. Start at the top
and work down; each guide is self-contained.

| Guide | Read it when you want to… |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | Install, run, and see search working in ~10 minutes. |
| [BRING-YOUR-OWN-CORPUS.md](BRING-YOUR-OWN-CORPUS.md) | Replace the demo libraries with your own content. |
| [LIBRARY-AUTHORING.md](LIBRARY-AUTHORING.md) | Author a domain library with the three-lens framework. |
| [SKILL-AUTHORING.md](SKILL-AUTHORING.md) | Add skill packages the engine can index and surface. |
| [MCP-WIRING.md](MCP-WIRING.md) | Wire the MCP server into Claude Desktop, Cursor, or Continue. |
| [EMBEDDINGS.md](EMBEDDINGS.md) | Turn on semantic (bge-m3) search alongside FTS5. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Run the engine beyond your laptop — server, hardening, backups. |
| [CONFIGURATION.md](CONFIGURATION.md) | Look up every environment variable, its type and default (generated). |
| [THEMING.md](THEMING.md) | Restyle or rebuild the dashboard. |
| [FAQ.md](FAQ.md) | Quick answers to the common questions. |

## The 30-second model

- **Corpus** — your markdown, in `corpus/`. This is data; you own it.
- **Engine** — the Python package in `engine/`. Indexes the corpus, serves an
  API, runs an MCP server. MIT-licensed; fork it freely.
- **Dashboard** — `engine/src/knowledge_engine/server/static/index.html`. A thin client over the engine's HTTP API.

You bring the corpus. The engine makes it searchable — in a browser, from the
CLI, and from any MCP-capable AI assistant.

## The happy path

```
install  →  bootstrap  →  reindex  →  serve  →  search
```

`install` sets up a virtualenv and installs the engine. `bootstrap` registers
every library/skill folder it finds. `reindex` builds the full-text index.
`serve` starts the dashboard + API. Then you search — in the browser, on the
CLI (`knowledge-engine search "query"`), or via MCP.

See [QUICKSTART.md](QUICKSTART.md) for the step-by-step.
