-- Migration 003 — FTS5 search index for project docs.
-- Contentless external-content style: rowid mirrors project_docs.rowid so the
-- ingestion pipeline can upsert/delete by rowid. Populated explicitly by the
-- pipeline (no triggers), matching the engine's "maintain on write" approach.

CREATE VIRTUAL TABLE IF NOT EXISTS project_docs_fts USING fts5(
    searchable_body,
    summary,
    content='',
    tokenize='porter unicode61'
);
