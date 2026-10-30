# ADR 0014 — Module catalog in its own `modules.db`; script wire protocol v1

- **Status:** Accepted (V2 P5, issue #37)
- **Date:** 2026-10

## Context

ADR 0009 fixed *what* the module catalog persists (intent and configuration
apart from observed health) but not *where*. Two existing stores were
candidates: the pipeline DB (`runtime.foundation.db`, home of the tool host's
`hosted_tools`) and `knowledge.db`. Script modules also need a stable
contract with the host.

## Decision

- The catalog lives in its own SQLite store, `modules.db` (`KE_MODULES_DB`,
  default `<KE_DATA_DIR>/modules.db`), with numbered migrations in
  `knowledge_engine/modules/migrations/` applied by the shared runner. A
  newer schema than the binary knows is refused (`incompatible`).
  - Not the pipeline DB: its connections follow the board's per-scope
    `ContextVar` routing (`using_db`), so a catalog there could silently be
    read from a board scope database. Executable capability must not depend
    on board scoping.
  - Not `knowledge.db`: that store owns source metadata (records, revisions,
    evidence), and the concurrent evidence slice (P3) owns its next
    migrations. The module manager owns executable capabilities; the two meet
    only through the ingestion interface (ADR 0015).
- Artifact bytes are stored content-addressed next to the catalog
  (`module-artifacts/<hh>/<sha256>`); their records live in `modules.db`.
- Script wire protocol v1: one JSON request on stdin (`protocol`, `module_id`,
  `module_version`, `tool`, `run_id`, `input`, `settings`, `scope`,
  `artifacts`, `now`), one JSON object on stdout (`{"output", "artifacts"}` or
  `{"error": {"code", "message"}}`). The argv comes from the manifest and is
  validated with the tool host's rules (allowlisted interpreter, script inside
  the module directory, no inline-code flags); `python` means the engine's own
  interpreter. The child gets only `PATH`, locale and temp-dir variables, its
  declared secret references, `KNOWLEDGE_ENGINE_MODULE_ID` and
  `KNOWLEDGE_ENGINE_RUN_ID`.

## Consequences

- Backups and retention of `modules.db` are separate from `knowledge.db` and
  the pipeline DB; deleting `modules.db` forgets registrations, not content.
- A script module is portable across hosts that allowlist its interpreter.
