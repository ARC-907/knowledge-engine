-- Migration 002 — Project content DB: normalized documentation records.
-- project_fp / branch_fp are plain columns (identity lives in the separate
-- fingerprint registry DB, so no cross-database foreign keys are possible).

CREATE TABLE IF NOT EXISTS project_doc_ingestion_runs (
    ingestion_run_id TEXT PRIMARY KEY,
    project_fp       TEXT NOT NULL,
    branch_fp        TEXT NOT NULL,
    mode             TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    stats_json       TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS project_docs (
    record_id              TEXT PRIMARY KEY,
    pointer_id             TEXT,
    project_fp             TEXT NOT NULL,
    branch_fp              TEXT NOT NULL,
    project_name           TEXT NOT NULL DEFAULT '',
    branch_name            TEXT NOT NULL DEFAULT '',
    source_path            TEXT NOT NULL DEFAULT '',
    source_uri             TEXT,
    category               TEXT NOT NULL,
    subtype                TEXT NOT NULL DEFAULT '',
    content_hash           TEXT NOT NULL,
    sanitized_content_hash TEXT,
    raw_retained           INTEGER NOT NULL DEFAULT 0,
    sanitization_status    TEXT NOT NULL DEFAULT 'sanitized',
    ingestion_status       TEXT NOT NULL DEFAULT 'ingested',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    source_modified_at     TEXT,
    git_commit             TEXT,
    git_branch             TEXT,
    git_dirty_json         TEXT,
    summary                TEXT NOT NULL DEFAULT '',
    ingestion_run_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_branch ON project_docs(branch_fp);
CREATE INDEX IF NOT EXISTS idx_docs_category ON project_docs(category);
CREATE INDEX IF NOT EXISTS idx_docs_path ON project_docs(source_path);
CREATE INDEX IF NOT EXISTS idx_docs_commit ON project_docs(git_commit);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON project_docs(content_hash);
CREATE INDEX IF NOT EXISTS idx_docs_run ON project_docs(ingestion_run_id);

CREATE TABLE IF NOT EXISTS project_doc_bodies (
    record_id       TEXT PRIMARY KEY REFERENCES project_docs(record_id) ON DELETE CASCADE,
    searchable_body TEXT NOT NULL DEFAULT '',
    raw_body        TEXT
);

CREATE TABLE IF NOT EXISTS project_doc_summaries (
    record_id  TEXT NOT NULL REFERENCES project_docs(record_id) ON DELETE CASCADE,
    summary    TEXT NOT NULL,
    summarizer TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    PRIMARY KEY (record_id)
);

CREATE TABLE IF NOT EXISTS project_doc_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    src_record_id TEXT NOT NULL REFERENCES project_docs(record_id) ON DELETE CASCADE,
    dst_record_id TEXT NOT NULL,
    link_type     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_src ON project_doc_links(src_record_id);

CREATE TABLE IF NOT EXISTS project_doc_provenance (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id        TEXT NOT NULL REFERENCES project_docs(record_id) ON DELETE CASCADE,
    ingestion_run_id TEXT,
    detector         TEXT,
    source_path      TEXT,
    source_span_json TEXT,
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS idx_prov_record ON project_doc_provenance(record_id);
