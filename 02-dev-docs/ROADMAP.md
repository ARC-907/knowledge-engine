# Roadmap — V2 planning revision

Updated 2026-10-30. V2 names this roadmap revision; the engine package remains
3.0.0. The modernization is substantially complete; the capabilities below
are proposed follow-on work, not implemented features.

The implementation contract is [V2-DESIGN.md](V2-DESIGN.md). It covers scoped
knowledge organization, evidence and citations, modular utilities, and optional
Serena integration. The table below is the development order; the historical
3.0 train remains recorded afterward.
The live tracking issue is
[#1](https://github.com/ARC-907/knowledge-engine/issues/1); this page is the
in-repository record of the same plan. Release mechanics are in
[RELEASING.md](RELEASING.md); the reasoning behind the larger decisions is in
the [ADRs](adr/README.md).

## Modernization baseline (historical report)

| Item | State |
| --- | --- |
| Code and docs for 3.0.0 | Merged on `main` |
| Package version | `3.0.0` (`engine/pyproject.toml`) |
| `v3.0.0` tag and GitHub Release | **Not published** (held by the maintainer) |
| Test suite | Prior train report: 1177 passed, 2 opt-in browser tests skipped; not rerun for this planning revision |
| CI | Prior train report: green on `main`; check the exact candidate's current runs before promotion |


## V2 delivery sequence

No dates or automatic version promises are assigned. Each row is a reviewable
implementation slice with a responsible component and an observable exit gate.
Maintainers assign contributors through the linked issues. New additive features
are candidates for a future minor release; breaking changes require the
compatibility/deprecation process and an explicit version decision.

| Step | Component / deliverable | Depends on | Exit gate | Tracking |
| --- | --- | --- | --- | --- |
| R0 | Retrieval: reproduce nested ownership discrepancy and decide ownership/membership | None | Regression fixture for FTS/vector/hybrid; documented compatibility effect | [#39](https://github.com/ARC-907/knowledge-engine/issues/39) |
| P0 | Core contracts: scope/taxonomy, source identity, adapter/error schemas and proposed ADRs | None; incorporate R0 decision | Two-project fixtures and explicit defaults approved; legacy snapshots preserved | [#35](https://github.com/ARC-907/knowledge-engine/issues/35) |
| P1 | Source-owned metadata: additive scope/collection migration and resumable backfill | P0 | Dry run, backup, upgrade/restart/rerun reconciliation and restore instructions | #35 |
| P2 | Retrieval: corpus and project-context adapters, broker and HTTP/CLI/MCP bindings | P1, R0 | Filters before ranking/limit, deterministic fusion, partial-failure and scope parity tests | #35 |
| P3 | Evidence: revisions, locators, derivation lineage and exact-revision open | P1 | Reindex/edit/delete/rename citation resolution fixtures pass | [#36](https://github.com/ARC-907/knowledge-engine/issues/36) |
| P4 | Citations: validation, Markdown references and CSL-JSON export | P2, P3 | Unknown metadata stays absent; unsupported claims/evidence reported; scope checked on export | #36 |
| P5 | Modules: manifest schema, catalog/lifecycle, shared dispatcher and MCP discovery | P0 | Two synthetic modules callable without loader edits; schema, collision, timeout and cancellation checks | [#37](https://github.com/ARC-907/knowledge-engine/issues/37) |
| P6 | Utilities: inventory and wrap media/news and transcription programs; host ingestion | P3, P5; actual utility interfaces available | End-to-end artifact -> scoped source -> evidence -> citation; idempotent restart | #37 |
| P7 | Serena: optional pinned external MCP adapter and project routing | P2, P3, P5 | Allowlisted reads, two-project concurrency/reconnect, code citations, unavailable-source behavior | [#38](https://github.com/ARC-907/knowledge-engine/issues/38) |
| P8 | Dashboard: scope/collection/type filters, evidence navigation and module status | P2, P4, P5 | Browser smoke exercises filtering, citation open and degraded module state | [#40](https://github.com/ARC-907/knowledge-engine/issues/40) |
| T1 | Tooling: Nox sessions mirroring CI, Serena project config for contributors, JSON logs with a Loki/Grafana stack, repository self-indexing with a local embedding model | None (independent) | `nox` reproduces CI locally; Grafana shows engine logs from Loki; the repository is searchable in its own engine with a local model | This roadmap |
| P9 | Release qualification: docs, migration guide, clean install and compatibility matrix | P4, P6, P7, P8 | All relevant gates in V2-DESIGN pass on the exact candidate | Linked implementation issues |

### Status and execution plan

Updated 2026-10-30. Slices land one at a time, each through a draft PR, the
local gate (ruff, mypy, pytest on 3.12/3.13/3.14, build and wheel smoke) and
green CI before a squash merge. Bugs found along the way are recorded in
[BUGLOG.md](BUGLOG.md); decisions and progress notes in [DEVLOG.md](DEVLOG.md);
user-visible changes under `[Unreleased]` in the [CHANGELOG](../CHANGELOG.md).

| Order | Step | State |
| --- | --- | --- |
| 1 | R0 nested-entry ownership | Merged in [#41](https://github.com/ARC-907/knowledge-engine/pull/41) |
| 2 | #40 browser CI job | Merged in [#42](https://github.com/ARC-907/knowledge-engine/pull/42) |
| 3 | P0 + P1 contracts and metadata store | Merged in [#43](https://github.com/ARC-907/knowledge-engine/pull/43) |
| 4 | P2 retrieval broker and bindings ([ADR 0011](adr/0011-retrieval-broker-fusion-and-adapters.md)) | Merged in [#45](https://github.com/ARC-907/knowledge-engine/pull/45) |
| 5 | P3 + P4 evidence and citations ([ADR 0012](adr/0012-evidence-store-and-ingestion.md), [ADR 0013](adr/0013-citation-identity-and-export.md)) | Merged in [#47](https://github.com/ARC-907/knowledge-engine/pull/47) |
| 6 | P5 + P6 modules, reference utilities, ingestion ([ADR 0014](adr/0014-module-catalog-store-and-script-protocol.md), [ADR 0015](adr/0015-module-jobs-cancellation-and-ingestion.md), [MODULES.md](../01-user-docs/MODULES.md)) | Implemented; PR in review |
| 7 | T1 tooling, observability and self-indexing ([DEV-TOOLING.md](DEV-TOOLING.md)) | Merged in [#46](https://github.com/ARC-907/knowledge-engine/pull/46) |
| 8 | P7 Serena adapter (compatibility probe of Serena 2.1.0 done) | After 4–6 |
| 9 | P8 dashboard ([DASHBOARD.md](../01-user-docs/DASHBOARD.md)) | Implemented; PR in review |
| 10 | P9 docs, migration guide and qualification | Last |

Publishing (tag `v3.0.0` or later, GitHub Release, repository settings) stays a
maintainer decision and is not part of this plan.

P0 and R0 may proceed together, but P0 cannot close without the ownership
decision. After P1, retrieval and evidence work can proceed independently.
P5 can proceed once P0 is settled. Browser CI enablement in #40 is independent
and should precede the new dashboard work. Serialize schema changes and shared
catalog mutations; parallel implementation uses separate branches.

### Definition of roadmap implementation complete

- Scope, source ownership, collections and research types remain distinct; an
  explicit project query cannot silently broaden into global retrieval.
- Search/open/citation references preserve source and exact revision.
- At least two modular utilities are discoverable and callable through the shared
  surface, with ingestion and provenance demonstrated using synthetic fixtures.
- Serena support has a pinned compatibility witness and works as an optional
  adapter; lean corpus operation has no Serena dependency.
- Migration, restart, rollback, interface compatibility and public documentation
  meet the gates in [V2-DESIGN.md](V2-DESIGN.md).
- Every claimed implementation result links an exact PR/commit and validation
  result. Open plans remain labelled proposed until that evidence exists.

### Risks and deferred work

Scope mistakes, revision loss, unconstrained module execution, and stateful
Serena project activation are the main architectural risks. Their controls and
failure tests are specified in the design. Collections alone are not an access
control system, and citation formatting alone is not evidence verification.

Defer automatic module installation, in-process Python plugins, arbitrary code
execution forwarding, Serena editing, and vector-backend replacement until
the relevant compatibility, policy and measured performance needs are established.
The existing 3.1 candidates below remain backlog, not prerequisites for this plan.


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

Publication remains a maintainer decision. This planning revision does not tag,
release, merge, delete branches or change repository settings. Recheck the exact
candidate and release prerequisites before taking these actions.

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
3. **Branch cleanup.** After confirming merge status and preservation of needed
   work, remove obsolete merged branches or enable automatic head-branch deletion.
   Preserve the active draft branch until its work is integrated.
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
- The Playwright dashboard tests run in the `dashboard (browser smoke)` CI job
  ([#40](https://github.com/ARC-907/knowledge-engine/issues/40)); locally they stay opt-in
  (`KE_UI_TESTS=1`).