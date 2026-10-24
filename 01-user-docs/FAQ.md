# FAQ

Short answers to the questions that come up most. Longer treatments are in the
linked guides.

## Is this a SaaS? Do I need an account?

No. Knowledge-Engine is **self-hosted software**. You run it on your own
machine or your own server. There is no account, no cloud service, and no data
leaves your infrastructure unless you point a provider at a remote endpoint
yourself. You get a repository; you run it.

## Does it work on Windows?

Yes. Windows, macOS and Linux are all supported, and the test suite runs on
all three in CI (Linux is the required job). The only requirement is `uv`,
which installs Python 3.12 or newer for you. The commands in the guides are
the same on every platform.

The one Windows-specific gotcha is in MCP client configs: backslashes in JSON
strings must be doubled (`\\`), or you can use forward slashes. See
[MCP-WIRING.md](MCP-WIRING.md).

## What do I actually need installed?

`uv` and about 200 MB of disk. That is the whole list for the core engine —
no Node toolchain, no database server, no Docker. Optional features (semantic
search, the Ollama generation provider) need a local Ollama; see below.

## Does it have embeddings / semantic search?

Yes — as an **opt-in layer**. The default search is SQLite FTS5: fast,
lexical, keyword-based, zero extra dependencies. On top of that the engine
ships an embedding index (bge-m3 through Ollama by default) with `vector` and
`hybrid` search modes on the CLI, the HTTP API and the MCP `search` tool. Full
guide: [EMBEDDINGS.md](EMBEDDINGS.md).

FTS5 and embeddings are independent — use either, both, or neither.

## Is it multi-user? Does it have authentication?

The core routes (`/search`, `/registry`, `/generate`) are **local-trust by
design**: the server binds to loopback, CORS allows loopback origins only, and
there is no login. This is the right model for a single developer or a small
trusted team on a private network.

The agent board is different: its admin routes always require an
`X-Board-Key`, and non-loopback writes can be key-gated too. If you need to
expose the engine to more people, put a reverse proxy with authentication in
front of it — see the hardening checklist in [DEPLOYMENT.md](DEPLOYMENT.md)
and the trust model in [`SECURITY.md`](../SECURITY.md). Authentication inside
the core app is not planned for 3.x.

## Can I deploy it to the cloud / a server?

Yes. The engine runs under uvicorn like any FastAPI app, so it deploys
anywhere Python runs. [DEPLOYMENT.md](DEPLOYMENT.md) covers running it as a
long-lived service, exposing it on a LAN, the hardening you must add before
exposing it to the internet (reverse proxy, TLS, authentication), and the
`Dockerfile` / `compose.yaml` that ship at the repository root.

## How big a corpus can it handle?

FTS5 is fast and the index is compact. A corpus of a few thousand markdown
files indexes in seconds and searches in milliseconds. Very large corpora
(tens of thousands of files and up) still work; reindex time grows linearly.
Embedding builds are the slow part — run them incrementally.

## Why does it ship with so little content?

It ships with **one demo library** (`corpus/samples/demo-library`) and **one
demo skill** (`corpus/samples/demo-skill`) — enough that search returns real
results the moment you install it. The engine is corpus-agnostic on purpose:
the value is the engine plus the three-lens organisational method, not a
pre-built knowledge base you would have to discard. See
[BRING-YOUR-OWN-CORPUS.md](BRING-YOUR-OWN-CORPUS.md).

## What's the three-lens framework?

The organisational method the demo library is built on: every library declares
three domain-tuned "lenses" (stages of thought) that make the corpus navigable
for people and agents alike — the demo library declares **CONCEPT → PATTERN →
APPLY**. Read the demo library's `README.md` to see it, then
[LIBRARY-AUTHORING.md](LIBRARY-AUTHORING.md) to apply it.

## How do I connect it to an MCP client?

The engine ships an MCP server (`knowledge-engine mcp`). You add a small
config block to your MCP client pointing at it. [MCP-WIRING.md](MCP-WIRING.md)
has copy-paste configs for Claude Desktop, Cursor and Continue, a generic
recipe for any other client, and the streamable-HTTP variant for clients that
connect over the network.

## Where does the engine write its data?

To `<KE_HOME>/.knowledge-engine/data` by default, where `KE_HOME` is the
nearest directory at or above your working directory that contains
`corpus/registry.json`. The FTS5 index (`index.db`), the embedding index
(`embeddings.db`), the pipeline/board database and per-scope board databases
all live there. Set `KE_DATA_DIR` to move it;
[CONFIGURATION.md](CONFIGURATION.md) lists every variable.

## Can I modify the engine? What's the license?

The whole project — engine, dashboard, docs and demo corpus — is
**MIT-licensed**: modify, fork, redistribute, use commercially, no
restrictions. See [`LICENSE`](../LICENSE). Contributions are welcome; see
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

## What support do I get?

This FAQ plus the guides in [`01-user-docs/`](README.md) answer most
questions. Beyond that it is open-source software — read the code, open an
issue, or fork it. Security reports go through the private channel described
in [`SECURITY.md`](../SECURITY.md).

## How is it versioned?

Semantic versioning across the engine package, the MCP tool surface and the
registry schema. Renamed modules keep a compatibility shim for one major
version. Releases are GitHub Releases on `vX.Y.Z` tags; the history is in
[`CHANGELOG.md`](../CHANGELOG.md), including a migration guide for each major.

## Something's broken / behaving oddly

Start with the troubleshooting table at the bottom of
[QUICKSTART.md](QUICKSTART.md) (install/run issues), [MCP-WIRING.md](MCP-WIRING.md)
(MCP client issues) or [EMBEDDINGS.md](EMBEDDINGS.md) (embedding errors).
`knowledge-engine info` prints the resolved paths and `knowledge-engine config
show` the effective configuration — the first things to check when paths look
wrong. Add `-v` to any command for INFO-level logs on stderr.
