-- knowledge.db schema version 1: scoped-knowledge metadata (V2 P1).
-- Consumers: knowledge_engine.knowledge.store, knowledge_engine.knowledge.backfill.
-- Identity columns are stable strings (never row ids). Never edit this file once
-- shipped; add 002_*.sql instead.

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id  TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Display names are deliberately NOT unique: two projects may share a name.
CREATE TABLE IF NOT EXISTS projects (
    project_id    TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    display_name  TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id);

CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    kind          TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    config_json   TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_workspace ON sources(workspace_id);

CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    project_id    TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
    display_name  TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collections_workspace ON collections(workspace_id);

-- One row per source-owned record; classification lives on the record.
CREATE TABLE IF NOT EXISTS records (
    source_id       TEXT NOT NULL REFERENCES sources(source_id) ON DELETE RESTRICT,
    record_id       TEXT NOT NULL,
    project_id      TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
    logical_path    TEXT,
    canonical_uri   TEXT,
    title           TEXT NOT NULL DEFAULT '',
    research_type   TEXT NOT NULL DEFAULT 'other' CHECK (research_type IN
        ('reference', 'paper', 'article', 'transcript', 'code', 'note', 'dataset',
         'synthesis', 'other')),
    curation_status TEXT NOT NULL DEFAULT 'unclassified' CHECK (curation_status IN
        ('unclassified', 'inbox', 'reviewed', 'archived')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (source_id, record_id),
    CHECK (logical_path IS NOT NULL OR canonical_uri IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_path
    ON records(source_id, logical_path) WHERE logical_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_records_project ON records(project_id);

CREATE TABLE IF NOT EXISTS record_tags (
    source_id  TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (source_id, record_id, tag),
    FOREIGN KEY (source_id, record_id) REFERENCES records(source_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_record_tags_tag ON record_tags(tag);

-- Many-to-many: a record may belong to several collections.
CREATE TABLE IF NOT EXISTS collection_members (
    collection_id TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    added_at      TEXT NOT NULL,
    PRIMARY KEY (collection_id, source_id, record_id),
    FOREIGN KEY (source_id, record_id) REFERENCES records(source_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_collection_members_record
    ON collection_members(source_id, record_id);

-- Named default selections (QueryContext dimensions a query omits).
CREATE TABLE IF NOT EXISTS scope_profiles (
    workspace_id   TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    profile_id     TEXT NOT NULL,
    profile_json   TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (workspace_id, profile_id)
);

-- Resumable backfill runs: one checkpoint per run, committed with each batch.
CREATE TABLE IF NOT EXISTS backfill_runs (
    run_id        TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    started_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    finished_at   TEXT,
    last_key      TEXT,
    processed     INTEGER NOT NULL DEFAULT 0,
    created       INTEGER NOT NULL DEFAULT 0,
    existing      INTEGER NOT NULL DEFAULT 0,
    batches       INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    backup_path   TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_backfill_runs_source ON backfill_runs(source_id, status);
