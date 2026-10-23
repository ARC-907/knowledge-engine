-- 004 folder mounts, folder permissions, prompt guards
--
-- Consumers: none inside the engine. Kept (and always created) for
-- downstream pipelines that expose node-local folders to workers and gate
-- prompts with guard rules.
--
-- Part of the pre-versioning baseline: statements must stay idempotent.

CREATE TABLE IF NOT EXISTS folder_mounts (
    mount_id TEXT PRIMARY KEY,
    mount_path TEXT NOT NULL UNIQUE,
    local_path TEXT NOT NULL,
    node_id TEXT NOT NULL,
    read_only INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS prompt_guards (
    guard_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    pattern TEXT,
    action TEXT NOT NULL DEFAULT 'block',
    scope TEXT DEFAULT 'all',
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS folder_permissions (
    perm_id TEXT PRIMARY KEY,
    folder_path TEXT NOT NULL,
    principal_type TEXT NOT NULL,
    principal_value TEXT NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read',
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_fperm_folder ON folder_permissions(folder_path);
