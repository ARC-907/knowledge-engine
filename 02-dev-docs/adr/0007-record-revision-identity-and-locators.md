# ADR 0007 — Canonical record/revision identity and locator units

- **Status:** Accepted (V2 P0, issue #35)
- **Date:** 2026-10

## Context

Search hits must become durable evidence and citations
([V2-DESIGN.md](../V2-DESIGN.md), "Source identity, evidence and
citations"). SQLite row ids and chunk ordinals change on reindex; content
hashes identify bytes, not ownership; locators without fixed units are
ambiguous.

## Decision

- **Records:** identity is `(source_id, record_id)`. `source_id` is namespaced
  (`<namespace>:<name>`, e.g. `corpus:base`); `record_id` is chosen by the
  owning source and never a row id. Corpus files use
  `file-<first 32 hex of sha256(corpus-relative POSIX path)>`
  (`knowledge_engine.knowledge.backfill.corpus_record_id`); the id is stored, so
  a later rename (P3) updates aliases without changing it.
- **Revisions:** `rev_<32 hex>` derived from source id, record id and the
  SHA-256 digest of the retained bytes
  (`knowledge_engine.knowledge.contracts.derive_revision_id`). Identical bytes
  in two records are two revisions; reindexing never changes the id.
- **Evidence spans:** `ev_<32 hex>` derived from revision id, the canonical
  JSON of the locator and the excerpt digest
  (`knowledge_engine.knowledge.contracts.derive_evidence_id`).
- **Locators** are a tagged union (`kind`). Every range is **half-open**
  `[start, end)`:
  - `text`: UTF-8 **byte** offsets into the exact retained revision bytes;
    `heading` is informational;
  - `pdf`: 1-based `page`; optional `bbox` in PDF points, origin bottom-left;
  - `transcript`: integer milliseconds `start_ms`/`end_ms`; `speaker` only when
    known;
  - `code`: `repository_id`, exactly one of `commit` or `snapshot_digest`
    (a dirty tree is never a clean commit), repository-relative `path`,
    optional qualified `symbol`, 1-based lines with exclusive `end_line`;
  - `unsupported`: an explicit `reason` when no position can be recovered.
- **Versioning:** every top-level contract model carries `schema_version`
  (currently `1`); other versions are rejected. The JSON Schema is pinned in
  `engine/tests/snapshots/knowledge_contracts.json`.

## Consequences

- P3 stores revisions and spans under these ids; P4 cites them; P7 maps Serena
  positions onto `code` locators (converting LSP's 0-based lines).
- Changing a derivation formula would change every id: it requires a new
  schema version and a migration, never an in-place edit.
