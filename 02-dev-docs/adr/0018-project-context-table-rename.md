# ADR 0018 — Rename the project-context document tables; no compatibility views

- **Status:** Accepted (3.1.0; roadmap "Known limitations in 3.0")
- **Date:** 2026-11

## Context

The project-context subsystem was renamed `project_docs` -> `project_context`
in 2.0.0, but its per-project content DB kept the old table names
`project_docs` and `project_docs_fts` to avoid a storage migration. The child
tables (`project_doc_bodies`, `project_doc_summaries`, `project_doc_links`,
`project_doc_provenance`, `project_doc_ingestion_runs`) and the foreign keys
in `doc_pointers` / `doc_embeddings` point at `project_docs`.

Findings that shaped the decision (SQLite 3.53, the engine's default
`legacy_alter_table = OFF`):

- `ALTER TABLE ... RENAME TO` on the base table moves its indexes and rewrites
  every `REFERENCES project_docs(...)` in the child tables, and the text of any
  trigger or view that names it.
- Renaming the FTS5 table renames its shadow tables (`_data`, `_idx`,
  `_docsize`, `_config`; the index is contentless, so there is no `_content`)
  and keeps its rowids, which mirror the document rowids.
- The shipped schema has no triggers or views: the ingestion pipeline maintains
  the FTS index explicitly. Nothing has to be recreated.
- The retrieval broker's project-context adapter opens content DBs read-only
  and never migrates them, so it can meet a store that has not run the rename
  yet.

## Decision

- Migration `008_project_documents.sql` renames `project_docs` ->
  `project_documents` and `project_docs_fts` -> `project_documents_fts`. It is
  plain DDL run by the shared runner in one `BEGIN IMMEDIATE` transaction with
  its `schema_version` row; a failure leaves the store at version 7 with the
  old names.
- Migrations 002-007 stay unchanged. Existing stores have recorded them, so
  rewriting them to create the new names would need a conditional rename that
  plain SQL migrations cannot express. Fresh and upgraded stores run the same
  sequence and end with the same `sqlite_master` (tested).
- The child tables keep their `project_doc_*` names: they already name the
  subsystem's records and renaming them would only widen the migration.
- **No compatibility views.** The content DB is internal storage: the CLI, the
  MCP tools and the retrieval broker are the supported interfaces, and no user
  or developer document tells anyone to query these tables. A `project_docs`
  view would keep the old name in every schema forever and would not help
  writers. There is no view for `project_docs_fts` either (an FTS5 `MATCH`
  cannot go through a view).
- Read-only readers resolve the names with
  `project_context.db.document_tables()`, which returns the legacy pair while
  `project_docs` still exists. The broker's project-context adapter and
  `project_context.search` use it; read-write code uses the new names, since
  every read-write open migrates first.

## Consequences

- Upgrading is automatic: the next read-write open of a content DB (any
  project-context MCP tool or CLI command) applies migration 008. Until then
  the broker keeps returning the same hits from the old tables.
- Downgrading to 3.0 after 008 has run is not supported: 3.0 queries
  `project_docs` and fails with "no such table". Restore a pre-upgrade backup
  instead.
- External scripts that queried `project_docs` / `project_docs_fts` directly
  must switch to the new names.
