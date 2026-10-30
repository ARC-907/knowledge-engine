# ADR 0015 — Module jobs, cancellation and the host-ingestion interface

- **Status:** Accepted (V2 P5/P6, issue #37)
- **Date:** 2026-10

## Context

Transcription and downloads can run for minutes; an MCP request must not be
held open for them, and cancelling must actually stop the work. Module
artifacts must reach the evidence store (V2 P3) with explicit scope and
provenance, but P3 was built concurrently and was not on this slice's base
when the interface was designed.

## Decision

- A tool declared `long_running` returns `{"status": "accepted", "job": {...}}`
  at once. Jobs are rows in `modules.db`, scoped to the workspace of the call
  (another workspace sees `unknown_scope`), expire `limits.job_ttl_s` after
  they finish, and keep their concurrency slot while running. MCP follows up
  with `module_job_status` / `module_job_cancel`; the CLI waits for the job it
  starts (its process owns it).
- Every script call runs in its own process group (`tool_host.host.run_bounded`).
  Timeout, cancellation, output overflow and disable stop the whole group
  (SIGTERM, then SIGKILL); processes left in the group are killed when the
  module exits. A cancel or disable issued by another process (CLI against a
  server) is a catalog flag the owning process polls. Service calls are bounded
  by their deadline; the host never kills external services it does not own.
- Retries happen only for tools declared `idempotent`, at most
  `limits.max_retries` times, after retryable failures.
- Host ingestion is an interface, `EvidenceIngestion.ingest(artifacts, *,
  destination, derivation)`: normalized artifacts with their bytes, an explicit
  `RecordScope`, and `DerivationInput` (run id, module id/version, parameters
  digest, parent artifact and revision ids). It returns a receipt with one
  revision per artifact and the contract `Derivation`. The default
  implementation answers `unavailable`; `InMemoryIngestion` implements P3's
  identity rules (same record + bytes = same revision) for tests.
- The production adapter, `EvidenceStoreIngestion`, maps the interface onto
  P3's `EvidenceStore` (added once P3 merged):
  - one `module` source per module and workspace, registered on first use;
    `module:<module_id>` in the default workspace and
    `module:<module_id>--<workspace_id>` elsewhere (module ids never contain
    `--`; an over-long id uses a digest of the workspace id);
  - each artifact is one `IngestRequest` with the run's derivation and the
    idempotency key `module:<artifact_id>:<run_id>`, which makes a restarted
    run replay and gives `revisions_for` a durable artifact-to-revision lookup
    without a new table;
  - the destination workspace and project are checked first; a new record is
    assigned to the destination project, and a record already in another
    project is refused rather than moved.

## Consequences

- Jobs owned by a process that dies stay `running` until they expire.
- Wiring P3 was one adapter class passed to `ModuleHost(ingestion=...)` by the
  lifespan (sharing the app's knowledge store), the CLI and the process-default
  host (both opening `knowledge.db` lazily); the only dispatcher change closes
  an ingestion store the host owns.
- Ingestion is atomic per artifact, not per run. A run interrupted between
  artifacts leaves the committed ones in place; repeating it completes the rest.
