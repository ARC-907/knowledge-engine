# ADR 0015 — Module jobs, cancellation and the host-ingestion interface

- **Status:** Accepted (V2 P5/P6, issue #37)
- **Date:** 2026-10

## Context

Transcription and downloads can run for minutes; an MCP request must not be
held open for them, and cancelling must actually stop the work. Module
artifacts must reach the evidence store (V2 P3) with explicit scope and
provenance, but P3 is built concurrently and is not on this slice's base.

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
  identity rules (same record + bytes = same revision) for tests. P3 supplies
  the real adapter.

## Consequences

- Jobs owned by a process that dies stay `running` until they expire.
- Wiring P3 is one adapter class passed to `ModuleHost(ingestion=...)` (and to
  the lifespan/CLI factories); no dispatcher change.
