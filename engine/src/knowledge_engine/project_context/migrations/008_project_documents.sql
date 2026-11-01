-- Migration 008 — Rename the document tables to match the subsystem name
-- (project_docs -> project_documents, project_docs_fts -> project_documents_fts).
-- See ADR 0018. Migrations 002-007 are left untouched: they are already
-- recorded in existing databases, so fresh and upgraded databases reach the
-- same schema by running the same sequence.
--
-- Runs in the runner's single BEGIN IMMEDIATE transaction, so a failure
-- leaves the database at version 7 with the old names intact.
--
-- * Indexes follow their table. Foreign keys in the child tables
--   (project_doc_bodies, project_doc_summaries, project_doc_links,
--   project_doc_provenance, doc_pointers, doc_embeddings) are rewritten by
--   SQLite to REFERENCES "project_documents" (legacy_alter_table is off).
-- * Renaming the FTS5 table renames its shadow tables (_data, _idx,
--   _docsize, _config; it is contentless, so there is no _content) and keeps
--   the rowids, which mirror project_documents.rowid.
-- * The shipped schema has no triggers or views: the FTS index is maintained
--   explicitly by the ingestion pipeline, so nothing needs to be recreated.

ALTER TABLE project_docs RENAME TO project_documents;
ALTER TABLE project_docs_fts RENAME TO project_documents_fts;
