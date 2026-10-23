-- 003 batch jobs
--
-- Consumers: none inside the engine. Kept (and always created) for
-- downstream pipelines built on the opt-in runtime substrate that fan a job
-- out into items processed by the worker fleet.
--
-- Part of the pre-versioning baseline: statements must stay idempotent.

CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_by TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    total_items INTEGER DEFAULT 0,
    completed_items INTEGER DEFAULT 0,
    failed_items INTEGER DEFAULT 0,
    config_json TEXT DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_status ON batch_jobs(status);

CREATE TABLE IF NOT EXISTS batch_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batch_jobs(batch_id),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    assigned_node TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batchitems_batch ON batch_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_batchitems_status ON batch_items(status);
