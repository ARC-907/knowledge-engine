-- 007 context artifacts + pipeline runs
--
-- Consumers: none inside the engine. Kept (and always created) for
-- downstream pipelines: context_artifacts holds summaries / extracts derived
-- from chat conversations, pipeline_runs tracks multi-stage task sequences.
--
-- Part of the pre-versioning baseline: statements must stay idempotent.

CREATE TABLE IF NOT EXISTS context_artifacts (
    artifact_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_message_start TEXT,
    source_message_end TEXT,
    active INTEGER DEFAULT 1,
    created_by TEXT,
    meta_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON context_artifacts(conversation_id);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    pipeline_id TEXT PRIMARY KEY,
    pipeline_card_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    input_json TEXT NOT NULL DEFAULT '{}',
    stages_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_status ON pipeline_runs(status);
