# FAQ

Short answers to the questions that come up most. Longer treatments are in the
linked guides.

## Is this a SaaS? Do I need an account?

No. Knowledge-Engine is **self-hosted software**. You run it on your own
machine or your own server. There is no account, no cloud service, no
`knowledge-engine.com`, and no data ever leaves your infrastructure. You get a
repository; you run it.

## Does it work on Windows?

Yes. Windows is a first-class target. The install and serve scripts ship in
both PowerShell (`.ps1`) and POSIX (`.sh`) form, and the engine was verified on
Windows. macOS and Linux are equally supported. The only requirement is Python
3.10 or newer.

The one Windows-specific gotcha is in MCP client configs: backslashes in JSON
strings must be doubled (`\\`), or you can use forward slashes. See
[MCP-WIRING.md](MCP-WIRING.md).

## What do I actually need installed?

Python 3.10+ and about 200 MB of disk. That's the whole list for the core
engine — no Node toolchain, no database server, no Docker. Optional features
(semantic search) need Ollama; see below.

## Does it have embeddings / semantic search?

Yes — as an **opt-in layer**. The default search is SQLite FTS5: fast, lexical,
keyword-based, zero extra dependencies. On top of that, the engine ships a
bge-m3 embedding index with cosine search that you switch on when you want
"search by meaning." It needs a local Ollama install. Full guide:
[EMBEDDINGS.md](EMBEDDINGS.md).

FTS5 and embeddings are independent — use either, both, or neither.

## Is it multi-user? Does it have authentication?

No. v1.0 is **local-trust by design**: it assumes the person running it is the
person allowed to use it. There is no login, no user accounts, no role-based
access, and CORS is open. This is the right model for a single developer or a
small trusted team on a private network.

If you need to expose it to more people, you put a reverse proxy with
authentication in front of it — the engine does not gate access itself. See the
hardening checklist in [DEPLOYMENT.md](DEPLOYMENT.md). Multi-user auth inside
the app is on the roadmap, not in v1.0.

## Can I deploy it to the cloud / a server?

Yes. The engine runs under uvicorn like any FastAPI app, so it deploys
anywhere Python runs. [DEPLOYMENT.md](DEPLOYMENT.md) covers running it as a
long-lived service, exposing it on a LAN, and the hardening you must add before
exposing it to the internet (reverse proxy, TLS, authentication).

A `Dockerfile` and a `compose.yaml` ship at the repository root — see the
Docker section of [DEPLOYMENT.md](DEPLOYMENT.md).

## How big a corpus can it handle?

FTS5 is fast and the index is compact. A corpus of a few thousand markdown
files indexes in seconds and searches in milliseconds. There is no practical
small-scale limit. Very large corpora (tens of thousands of files and up) still
work; reindex time grows linearly. If you want semantic ranking at scale, layer
embeddings on top.

## Why does it ship with so little content?

It ships with **one demo library** (`corpus/samples/demo-library`) and **one
demo skill** — enough that search returns real results the moment you install
it. But the engine is corpus-agnostic on purpose: the *value* is the engine
plus the three-lens organizational methodology, not a pre-built knowledge base
you'd have to discard. You bring your own corpus; the engine makes it
searchable and agent-accessible. See [BRING-YOUR-OWN-CORPUS.md](BRING-YOUR-OWN-CORPUS.md).

## What's the three-lens framework?

It's the organizational methodology the demo library is built on: every
library declares three domain-tuned "lenses" (stages of thought) that make the
corpus navigable for both people and AI agents — the demo library declares
**CONCEPT → PATTERN → APPLY**. It's the discipline that turns a pile of
markdown into usable, structured research. Read the demo library's `README.md`
to see it, then [LIBRARY-AUTHORING.md](LIBRARY-AUTHORING.md) to apply it.

## How do I connect it to Claude / Cursor / Continue?

The engine ships an MCP server. You add a small config block to your MCP
client pointing at the engine. [MCP-WIRING.md](MCP-WIRING.md) has working,
copy-paste configs for Claude Desktop, Cursor, and Continue, plus a generic
recipe for any other MCP client.

## Can I modify the engine? What's the license?

The whole project — engine, dashboard, docs, and demo corpus — is
**MIT-licensed**: modify, fork, redistribute, use commercially, no
restrictions. See [`LICENSE`](../LICENSE).

## What support do I get?

This FAQ plus the guides in [`01-user-docs/`](README.md) are designed to answer
the large majority of questions without needing support at all. Beyond that
it's open-source software — read the code, open an issue, or fork it.

## Will there be updates?

The roadmap includes in-app multi-user authentication. Versioning follows semver across the engine package, MCP
protocol, and registry schema — see [CHANGELOG.md](../02-dev-docs/CHANGELOG.md).

## Something's broken / behaving oddly

Start with the troubleshooting table at the bottom of [QUICKSTART.md](QUICKSTART.md)
(install/run issues) or [MCP-WIRING.md](MCP-WIRING.md) (MCP client issues).
`knowledge-engine info` prints the resolved configuration, which is the first
thing to check when paths look wrong.
