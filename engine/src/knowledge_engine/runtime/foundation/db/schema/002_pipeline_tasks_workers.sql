-- 002 pipeline: task queue + worker registry
--
-- Consumers: runtime.pipeline.queue (tasks: lease-based claiming, audit
-- trail) and runtime.pipeline.worker_registry (workers: registration,
-- heartbeats, safeguards).
--
-- Part of the pre-versioning baseline: statements must stay idempotent.

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    library TEXT NOT NULL,
    target_path TEXT NOT NULL DEFAULT '',
    chapter_key TEXT NOT NULL,
    priority INTEGER DEFAULT 3,
    depends_on TEXT DEFAULT '[]',
    allowed_layer TEXT DEFAULT 'local',
    assigned_capability TEXT DEFAULT '[]',
    source_policy TEXT DEFAULT '{}',
    input_json TEXT NOT NULL DEFAULT '{}',
    meta_json TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    assigned_worker TEXT,
    lease_expires_at TEXT,
    claimed_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0,
    failure_reason TEXT,
    output_artifacts TEXT DEFAULT '[]',
    notes TEXT,
    audit_trail TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_library ON tasks(library);

CREATE TABLE IF NOT EXISTS workers (
    node_id TEXT PRIMARY KEY,
    hostname TEXT,
    tailscale_ip TEXT,
    display_name TEXT,
    model_class TEXT,
    role TEXT,
    capabilities TEXT DEFAULT '[]',
    ollama_endpoint TEXT,
    ollama_model TEXT,
    max_concurrent INTEGER DEFAULT 1,
    status TEXT DEFAULT 'offline',
    current_tasks TEXT DEFAULT '[]',
    last_heartbeat TEXT,
    registered_at TEXT,
    config_json TEXT DEFAULT '{}',
    safeguard_locked INTEGER DEFAULT 0,
    safeguard_reason TEXT,
    resources_json TEXT DEFAULT '{}'
);
