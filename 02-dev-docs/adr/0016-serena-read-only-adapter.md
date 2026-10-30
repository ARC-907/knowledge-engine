# ADR 0016 — Serena: read-only adapter, pinned witness, one process per project

- **Status:** Accepted (V2 P7, issue #38; supersedes the deferral in [ADR 0010](0010-serena-mapping-deferred.md))
- **Date:** 2026-10

## Context

The design asks for optional code-symbol retrieval through Serena with project
isolation, an exact tool mapping and no core dependency
([V2-DESIGN.md](../V2-DESIGN.md), "Serena integration"). A probe of Serena
2.1.0 over stdio MCP showed:

- its default `agent` context exposes only a code-execution REPL tool;
- it reports usage to its vendor unless `SERENA_USAGE_REPORTING=false`;
- it starts pyright through `uvx`;
- it writes a `.serena/` directory into every project it opens;
- it answers in JSON text with 0-based inclusive lines.

Serena is GPL-3.0-or-later.

## Decision

- **Not bundled.** Operators install Serena; `KE_SERENA_COMMAND` points at it.
  The adapter is off unless `KE_SERENA_ENABLED=1`. The engine speaks to
  Serena over stdio MCP only.
- **Launch.** One process per configured project id, started on first use with
  `--project <root>`. Launch flags:
  - `--agent-interface tools`, `--context agent`;
  - the dashboard and GUI turned off;
  - `HOME` isolated under the data dir;
  - `SERENA_USAGE_REPORTING=false`;
  - `uv` on `PATH`.

  `--project-from-cwd` and `activate_project` are never used.
- **Witness.** `knowledge_engine/serena/witness.json` records:
  - the server name and version;
  - the upstream tool list;
  - the SHA-256 of the canonical JSON of each mapped tool's input schema.

  A session whose server name or version (`KE_SERENA_VERSION`) or any mapped
  schema hash differs is `incompatible` and calls nothing; tools not mapped
  are not checked.
- **Allowlist.** Exactly `find_symbol`, `get_symbols_overview` and
  `find_referencing_symbols`, exposed as `serena.*` with the engine's own
  bounded schemas (a `project_id` plus typed arguments; only documented
  upstream argument names are forwarded). Every other upstream tool, present
  or future, is refused before routing.
- **Isolation.** Every incoming and outgoing path is contained in the project
  root (symlinks followed); escaping results are dropped and reported.
  Calls are serialized per session. Concurrency across projects is bounded by
  `KE_SERENA_MAX_SESSIONS`: at the cap, the least recently used idle session
  is stopped; if none is idle, the call fails `unavailable`.
- **Failures.**
  - A timeout or a crash stops the process.
  - Restarts back off exponentially (0, 1, 2 … 60 s); calls during the backoff
    fail with a retryable `unavailable`.
  - Idle sessions stop after `KE_SERENA_IDLE_SHUTDOWN_S`.
- **Locators.** Serena's lines `[s, e]` become `[s + 1, e + 2)`. The version
  is `git rev-parse HEAD` on a clean tree. A dirty tree uses a snapshot
  digest over the changed paths and `HEAD`, excluding Serena's `.serena/`
  state. A non-git root gives `unsupported` locators.
- **Broker source.** Each project is a `serena:<project_id>` source (kind
  `serena`) in its project's workspace.
  - It is searched only when `context.source_ids` names it, in `fts` mode. It
    accepts every mode so that an unselected source stays silent instead of
    making vector searches partial.
  - Its records are files (`f.<base32(path)>`) scoped to the project like
    project-context documents.
  - Opening a current revision records it in the evidence store with its
    commit or snapshot, so the citation service validates code evidence like
    any other. Older recorded revisions open as `stale`; unrecorded ones are
    `unsupported`.
- **Memories** are not imported.

## Consequences

- Corpus, project-context and module features are unaffected when Serena is
  absent, disabled or incompatible.
- A Serena upgrade needs `serena check`. Schema changes need a new witness
  (engine release); a version-only change can be accepted with
  `KE_SERENA_VERSION`.
- Code evidence is citable only after it was opened through the broker. That
  is the moment the engine reads the bytes, so citations never claim content
  the engine did not see.
- Paths longer than about 118 bytes get hashed record ids (`h.`); opening
  them needs the evidence store's record of the path.
