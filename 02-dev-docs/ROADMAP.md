# Roadmap

Where the 3.0 modernization stands, what is still open, and what comes next.
The live tracking issue is
[#1](https://github.com/ARC-907/knowledge-engine/issues/1); this page is the
in-repository record of the same plan. Release mechanics are in
[RELEASING.md](RELEASING.md); the reasoning behind the larger decisions is in
the [ADRs](adr/README.md).

## Status

| Item | State |
| --- | --- |
| Code and docs for 3.0.0 | Merged on `main` |
| Package version | `3.0.0` (`engine/pyproject.toml`) |
| `v3.0.0` tag and GitHub Release | **Not published** (held by the maintainer) |
| Test suite | 1177 passed, 2 opt-in browser tests skipped |
| CI | Green on `main`: lint, type check, test matrix, build, docs, zizmor, CodeQL |

## Starting point

A full audit of the 2.0 tree found:

- **Path regressions.** After the layering refactor, the default corpus and
  data directories resolved inside the package, and the dashboard was never
  mounted.
- **Version drift.** Three different version numbers were in use.
- **Open admin surface.** The agent board's admin routes were reachable
  without a key under the default settings, behind a wildcard CORS policy.
- **Duplication.** There were four Ollama clients, two embedding stacks, four
  `slugify` helpers and several configuration systems.
- **No tooling configuration.** There was no lockfile, formatter, type
  checking or release pipeline.
- **Stale docs.** Many documents referenced modules that no longer existed.

The baseline was 361 tests with one flaky. 82 of 137 files needed
reformatting, with about 1300 lint findings and about 220 strict type errors.

## Delivered: the 3.0 train

The work landed as an ordered series of pull requests, each validated by a
local gate and by CI before a squash merge. [ADR 0004](adr/0004-merge-train.md)
explains why. Merge order:

| # | Pull request | Theme |
| --- | --- | --- |
| 1 | [#16](https://github.com/ARC-907/knowledge-engine/pull/16) | uv + hatchling, Python 3.12+, ruff / mypy / coverage configuration |
| 2 | [#17](https://github.com/ARC-907/knowledge-engine/pull/17) | Release, CodeQL and Scorecard workflows; Dependabot; templates |
| 3 | [#20](https://github.com/ARC-907/knowledge-engine/pull/20) | Default paths from `KE_HOME`, dashboard as package data, version drift |
| 13 | [#21](https://github.com/ARC-907/knowledge-engine/pull/21) | Dockerfile and compose, `.gitignore`, internal-reference scrub |
| 5 | [#25](https://github.com/ARC-907/knowledge-engine/pull/25) | Git ref injection, pointer containment, tool-host argv/proxy, secret redaction |
| 4 | [#22](https://github.com/ARC-907/knowledge-engine/pull/22) | Board admin auth, loopback CORS default, proxy/body/scope hardening |
| 6 | [#24](https://github.com/ARC-907/knowledge-engine/pull/24) | Official MCP SDK v2, stdio and streamable HTTP |
| 12 | [#23](https://github.com/ARC-907/knowledge-engine/pull/23) | Compiled Tailwind dashboard, accessibility, light/dark theme |
| 7 | [#26](https://github.com/ARC-907/knowledge-engine/pull/26) | I/O-free app factory, dependency injection, typed `Settings`, real cloud provider |
| 8 | [#27](https://github.com/ARC-907/knowledge-engine/pull/27) | Shared utilities, one SQLite and Ollama client, CLI package |
| 9 | [#28](https://github.com/ARC-907/knowledge-engine/pull/28) | Registry JSON Schema validation, kits, `registry validate` |
| 11 | [#29](https://github.com/ARC-907/knowledge-engine/pull/29) | Unified embeddings, batch embedding, hybrid search |
| 10 | [#30](https://github.com/ARC-907/knowledge-engine/pull/30) | Versioned migrations, shared migration runner, typed substrate boundaries |
| 14 | [#31](https://github.com/ARC-907/knowledge-engine/pull/31) | Docs refresh, root CHANGELOG, CONTRIBUTING, Code of Conduct, ADRs |
| — | [#32](https://github.com/ARC-907/knowledge-engine/pull/32) | Release commit: version 3.0.0 |
| — | [#33](https://github.com/ARC-907/knowledge-engine/pull/33) | Review follow-ups: lifecycle-aware watcher, MCP limit bound, board health shape, cache bound, CI config check |

The [CHANGELOG](../CHANGELOG.md) `[3.0.0]` section lists every user-visible
change and includes a **Migrating from 2.x** guide.

## Remaining before 3.0.0 is public

These need repository permissions the automation does not have. They are
deliberately on hold until the maintainer decides to publish.

1. **Tag and release.** Tag the current `main` as `v3.0.0` and push the tag.
   The release workflow checks the tag against the package version, builds
   the wheel and sdist, attests provenance, and creates the GitHub Release
   from the CHANGELOG section ([RELEASING.md](RELEASING.md), steps 6–8). If
   more changes land first, add their lines to `[3.0.0]` or release them as
   3.0.1.
2. **Repository description and topics.** The About panel still describes a
   paid bundle. Suggested description: "Self-hosted, MCP-native knowledge
   engine for markdown: SQLite FTS5 + optional vector/hybrid search, FastAPI
   + dashboard, MCP over stdio and streamable HTTP." Suggested topics: `mcp`,
   `model-context-protocol`, `knowledge-base`, `sqlite-fts5`, `fastapi`,
   `markdown`, `self-hosted`, `hybrid-search`.
3. **Branch cleanup.** Delete the merged `train/*` branches and the session
   branch, or enable automatic deletion of head branches.
4. **Optional.**
   - A `main` ruleset: require a PR, require the lint, Ubuntu test matrix,
     build, zizmor and docs checks, and block force-push.
   - A retroactive `v2.0.0` tag on `dd3a155`, so the CHANGELOG compare link
     resolves. The release workflow refuses that tag because it does not
     match the package version, so nothing is published.

## Planned after 3.0.0

### 3.0.x (maintenance)

- Watch Dependabot updates. Actions and uv dependencies are grouped weekly
  with a seven-day cooldown.
- Fix forward on any regression report; never move a published tag.

### 3.1 candidates

- **Typed row shapes.** The queue, worker registry and message board return
  plain dictionaries checked with `require_row`. Typed row definitions
  (TypedDict or dataclasses) would let mypy check field access in the
  substrates.
- **PyPI distribution.** The name `knowledge-engine` belongs to an unrelated
  project. Publishing needs a free distribution name, a trusted publisher and
  the `PYPI_PUBLISH` repository variable ([RELEASING.md](RELEASING.md)).
- **Vector index backend.** Vector search streams every stored vector
  through a cosine scan (`CorpusEmbeddingIndex`). A pluggable
  approximate-nearest-neighbour backend such as sqlite-vec would be needed
  once corpora outgrow that.
- **Windows and macOS as required checks.** They run on every PR but are
  advisory, pending a longer green history.
- **Compatibility shim removal in 4.0.** `knowledge_engine.mcp_server` and
  the other 3.0 shims are removed one major version later
  ([CONTRIBUTING.md](../CONTRIBUTING.md), deprecation policy).

### Known limitations in 3.0

- The project-context subsystem keeps its historical `project_docs` /
  `project_docs_fts` SQLite table names. Renaming live tables needs its own
  migration.
- The Playwright dashboard tests are opt-in (`KE_UI_TESTS=1`) and no CI job
  runs them yet; adding an Ubuntu job for them is a 3.1 candidate.
