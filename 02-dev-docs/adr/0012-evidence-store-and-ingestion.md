# ADR 0012 — Evidence store layout, idempotent ingestion and rename detection

- **Status:** Accepted (V2 P3, issue #36)
- **Date:** 2026-10

## Context

Search hits must become durable evidence ([V2-DESIGN.md](../V2-DESIGN.md),
"Source identity, evidence and citations"). ADR 0007 fixed the identities
and locator units and ADR 0008 the retention vocabulary; this slice decides
where revisions live, how ingestion stays idempotent and atomic, and how
the corpus maps onto records over edits, renames and deletions.

## Decision

- **One owning store.** Revisions, retained bytes, aliases, evidence spans,
  derivations, tombstones, ingestion requests and an outbox live in
  `knowledge.db` (migration `002_evidence.sql`, schema version 2), next to the
  records they belong to, through the shared migration runner. `records`
  gains retention, license, bibliographic metadata, the current (head)
  revision and a deletion marker.
- **Identity.** `revision_id` and `evidence_id` are the ADR 0007 derivations;
  integer `seq` columns only order rows internally. Identical bytes in two
  records are two revisions. Re-ingesting the bytes of an earlier revision
  moves the head back to it instead of creating a new one.
- **One write path.** `EvidenceStore.ingest`
  (`knowledge_engine.knowledge.evidence.store`) validates size
  (`KE_EVIDENCE_MAX_BYTES`), media type, digest and source, creates or
  updates the record, writes the revision, bytes, spans (the host recomputes
  every excerpt from the bytes), derivation lineage and an outbox event in
  **one** SQLite transaction. A request with an `idempotency_key` is replayed
  when key and inputs match and refused (`invalid_input`,
  `idempotency_conflict`) otherwise; without a key, content addressing makes
  a repeat a no-op.
- **Cross-store work** (FTS/vector index generations) consumes the outbox and
  acknowledges events by sequence number; there is no pretend cross-database
  transaction.
- **Retention.** `retain` keeps bytes; `digest_only` keeps the digest plus an
  external locator and the reason; `tombstone` refuses new bytes. Oversized
  corpus files are recorded digest-only with the reason.
- **Deletion** follows `KE_EVIDENCE_ON_DELETE`: `tombstone` (default) removes
  bytes and ingestion records, keeps identity, digest, time, reason and the
  span locators so citations resolve `unavailable`; `purge` removes the record
  and everything derived from it.
- **Corpus snapshot.** `knowledge snapshot` records each corpus file: same
  digest is unchanged, new bytes a new revision, a vanished record whose head
  digest matches exactly one new path is a **rename** (path moves, old path
  becomes an alias, id unchanged); anything ambiguous is new + missing, never
  guessed. Missing files are deleted only with `--delete-missing` and never
  while a registry entry's content is missing. A new file at a renamed
  record's old path gets a suffixed corpus record id.
- **Open.** `open_evidence` (`knowledge_engine.knowledge.evidence.resolve`) is
  the exact-revision open the retrieval broker calls: scope recheck, exact
  revision, digest and span verification; it never follows to the latest
  revision.

## Consequences

- Schema version 2; an older binary refuses the store (`incompatible`).
- Renames combined with edits in one step are seen as delete + create; the
  old record keeps its revisions and citations stay resolvable (`unavailable`
  after deletion).
- Retained bytes grow `knowledge.db`; operators bound them with
  `KE_EVIDENCE_MAX_BYTES` and `KE_EVIDENCE_RETENTION=digest_only`.
