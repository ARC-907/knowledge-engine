-- Migration 001 — Fingerprint registry DB (shared across projects).
-- Identity lives here, separate from any ingested content, so project/branch
-- tracking is never entangled with documentation bodies.

CREATE TABLE IF NOT EXISTS projects (
    project_fp           TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    root_path            TEXT NOT NULL,
    remote_identity_hash TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    notes                TEXT
);

CREATE TABLE IF NOT EXISTS branches (
    branch_fp   TEXT PRIMARY KEY,
    project_fp  TEXT NOT NULL REFERENCES projects(project_fp) ON DELETE CASCADE,
    branch_name TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (project_fp, branch_name)
);
CREATE INDEX IF NOT EXISTS idx_branches_project ON branches(project_fp);

CREATE TABLE IF NOT EXISTS project_fingerprints (
    project_fp        TEXT PRIMARY KEY REFERENCES projects(project_fp) ON DELETE CASCADE,
    strategy          TEXT NOT NULL DEFAULT 'sha256-root',
    source_inputs_hash TEXT NOT NULL,
    manual_override   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branch_fingerprints (
    branch_fp       TEXT PRIMARY KEY REFERENCES branches(branch_fp) ON DELETE CASCADE,
    project_fp      TEXT NOT NULL,
    strategy        TEXT NOT NULL DEFAULT 'sha256-project-branch',
    manual_override INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprint_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- alloc_project | alloc_branch | override | collision | validate
    project_fp TEXT,
    branch_fp  TEXT,
    detail     TEXT,
    data_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fpevents_kind ON fingerprint_events(kind);
