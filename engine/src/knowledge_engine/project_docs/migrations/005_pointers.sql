-- Migration 005 — Docstring/record pointers and the rewrite-safety ledger.

CREATE TABLE IF NOT EXISTS doc_pointers (
    pointer_id       TEXT PRIMARY KEY,
    record_id        TEXT NOT NULL REFERENCES project_docs(record_id) ON DELETE CASCADE,
    scheme           TEXT NOT NULL DEFAULT 'ke-doc',
    ptype            TEXT NOT NULL DEFAULT 'doc',
    project_fp       TEXT NOT NULL,
    branch_fp        TEXT NOT NULL,
    source_path      TEXT,
    source_span_json TEXT,
    content_hash     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_pointers_record ON doc_pointers(record_id);

CREATE TABLE IF NOT EXISTS pointer_backrefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pointer_id  TEXT NOT NULL REFERENCES doc_pointers(pointer_id) ON DELETE CASCADE,
    ref_source_path TEXT NOT NULL,
    ref_span_json   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backrefs_pointer ON pointer_backrefs(pointer_id);

CREATE TABLE IF NOT EXISTS pointer_rewrite_plans (
    plan_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    dry_run    INTEGER NOT NULL DEFAULT 1,
    items_json TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'planned'
);

CREATE TABLE IF NOT EXISTS pointer_rewrite_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT REFERENCES pointer_rewrite_plans(plan_id) ON DELETE SET NULL,
    pointer_id  TEXT,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,        -- plan | apply | rollback | skip | error
    backup_path TEXT,
    result      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_rwevents_plan ON pointer_rewrite_events(plan_id);
