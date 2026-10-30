# ADR 0008 — Retention and tombstone policy

- **Status:** Accepted (V2 P0, issue #35)
- **Date:** 2026-10

## Context

Evidence wants reproducibility; privacy, rights and storage limits want
deletion ([V2-DESIGN.md](../V2-DESIGN.md), retention paragraph). The
contract must say what a citation resolves to after deletion.

## Decision

- Retention is **operator-configured** per record via
  `RightsInfo.retention`: `retain` (keep bytes), `digest_only` (keep the digest
  and an external locator, not the bytes) or `tombstone` (bytes and derived
  indexes removed).
- A revision without retained bytes says why (`Revision.bytes_retained=false`
  requires `unavailable_reason`).
- Deletion removes retained bytes and derived index rows and may leave a
  minimal tombstone (identity, digest, deletion time and reason) when policy
  permits. Resolving a citation to deleted or digest-only bytes yields
  `unavailable`; an `OpenResult` in that state carries no content.
- Privacy/rights deletion takes precedence over reproducibility. No surface
  promises indefinite retention.
- Backups taken by `knowledge migrate --apply` are operator-owned copies:
  deleting content must include old backups when policy requires it.

## Consequences

- P3 implements tombstones in the revision store; P4 reports `unavailable`
  citations instead of failing exports.
- This slice records only the vocabulary (`RetentionMode`) and the
  `OpenResult`/`Revision` validation rules.
