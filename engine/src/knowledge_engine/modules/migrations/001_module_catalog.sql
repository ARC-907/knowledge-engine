-- modules.db schema version 1: module catalog, observed health, runs, jobs and artifacts (V2 P5).
-- Consumers: knowledge_engine.modules.catalog.
-- Intent/configuration (modules) is persisted separately from observed health
-- (module_health), which is runtime state rebuilt on every host start (ADR 0009).
-- Never edit this file once shipped; add 002_*.sql instead.

CREATE TABLE IF NOT EXISTS modules (
    module_id        TEXT PRIMARY KEY,
    version          TEXT NOT NULL,
    manifest_json    TEXT NOT NULL,
    manifest_digest  TEXT NOT NULL,
    source_dir       TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 0,
    configured       INTEGER NOT NULL DEFAULT 0,
    settings_json    TEXT NOT NULL DEFAULT '{}',
    secret_env_json  TEXT NOT NULL DEFAULT '{}',
    grants_json      TEXT NOT NULL DEFAULT '[]',
    registered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS module_health (
    module_id    TEXT PRIMARY KEY REFERENCES modules(module_id) ON DELETE CASCADE,
    state        TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    observed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Sanitized audit trail: no inputs, outputs or credentials, only digests and ids.
CREATE TABLE IF NOT EXISTS module_runs (
    run_id            TEXT PRIMARY KEY,
    module_id         TEXT NOT NULL,
    module_version    TEXT NOT NULL,
    tool              TEXT NOT NULL,
    surface           TEXT NOT NULL,
    principal         TEXT NOT NULL DEFAULT '',
    workspace_id      TEXT NOT NULL,
    project_id        TEXT,
    outcome           TEXT NOT NULL,
    error_code        TEXT,
    elapsed_ms        INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 1,
    catalog_version   INTEGER NOT NULL DEFAULT 0,
    policy_json       TEXT NOT NULL DEFAULT '{}',
    params_digest     TEXT NOT NULL,
    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    job_id            TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_module_runs_module ON module_runs(module_id, started_at);

CREATE TABLE IF NOT EXISTS module_jobs (
    job_id            TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    module_id         TEXT NOT NULL,
    tool              TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    project_id        TEXT,
    owner             TEXT NOT NULL,
    state             TEXT NOT NULL,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    result_json       TEXT,
    error_json        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_module_jobs_scope ON module_jobs(workspace_id, created_at);

-- Normalized artifacts; bytes live content-addressed next to the database.
CREATE TABLE IF NOT EXISTS module_artifacts (
    artifact_id          TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    module_id            TEXT NOT NULL,
    module_version       TEXT NOT NULL,
    workspace_id         TEXT NOT NULL,
    project_id           TEXT,
    media_type           TEXT NOT NULL,
    digest               TEXT NOT NULL,
    byte_length          INTEGER NOT NULL,
    descriptor_json      TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_module_artifacts_run ON module_artifacts(run_id);
