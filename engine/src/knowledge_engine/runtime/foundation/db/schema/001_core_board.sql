-- 001 core / agent board
--
-- Consumers: agent_board (store, sweeper, keys, provider_registry, scopes),
-- runtime.pipeline.message_board, and the event log written by
-- runtime.foundation.db.log_event. Every per-scope board database
-- (<data_dir>/board-scopes/<scope>.db) carries the same tables.
--
-- Part of the pre-versioning baseline: statements must stay idempotent
-- (IF NOT EXISTS) because a pre-versioned database replays them on adoption.

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'ops',
    thread_id TEXT,
    task_id TEXT,
    product_id TEXT,
    sender_agent_id TEXT,
    sender_node_id TEXT,
    sender_role TEXT,
    model_id TEXT,
    message_type TEXT NOT NULL,
    subject TEXT,
    body TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    visibility_scope TEXT DEFAULT 'all',
    requires_ack INTEGER DEFAULT 0,
    ack_by TEXT DEFAULT '[]',
    reply_to TEXT,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(message_type);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
-- Composite index for sweeper / reminder / digest scans that filter
-- by message_type and order by created_at.
CREATE INDEX IF NOT EXISTS idx_messages_type_created
    ON messages(message_type, created_at);
-- Reply-to scans (reminder dedup, thread traversal).
CREATE INDEX IF NOT EXISTS idx_messages_reply_to
    ON messages(reply_to);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    node_id TEXT,
    agent_id TEXT,
    detail TEXT,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

-- Provider credential bindings (env-var names, never secrets).
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    display_name TEXT NOT NULL,
    env_var TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    last_verified TEXT,
    notes TEXT
);

-- Agent API keys (board access; auth for tool routes / MCP).
CREATE TABLE IF NOT EXISTS agent_api_keys (
    key_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'admin',
    enabled INTEGER DEFAULT 1,
    is_master INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_agentkeys_hash ON agent_api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_agentkeys_enabled ON agent_api_keys(enabled);
-- At most one enabled master key may exist. Defends against the
-- TOCTOU race in ensure_master_key when two concurrent bootstrap
-- requests both see "no master" and try to create one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agentkeys_one_master
    ON agent_api_keys(is_master)
    WHERE is_master = 1 AND enabled = 1;

CREATE TABLE IF NOT EXISTS agent_key_permissions (
    perm_id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL REFERENCES agent_api_keys(key_id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL DEFAULT '*',
    permission TEXT NOT NULL DEFAULT 'invoke',
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_akperm_key ON agent_key_permissions(key_id);
CREATE INDEX IF NOT EXISTS idx_akperm_resource ON agent_key_permissions(resource_type, resource_id);

-- Key-value store for sync state and misc config (sweeper lease).
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Sweeper audit trail. Each row = one pass of the prune+reminder loop.
CREATE TABLE IF NOT EXISTS board_sweeps (
    sweep_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pruned_expired INTEGER DEFAULT 0,
    pruned_overflow INTEGER DEFAULT 0,
    reminders_emitted INTEGER DEFAULT 0,
    digests_emitted INTEGER DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sweeps_started ON board_sweeps(started_at);

-- Singleton config row holding port, sweeper interval, retention overrides.
-- Seeded after the migrations run (runtime.foundation.db.seed_board_config).
CREATE TABLE IF NOT EXISTS board_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    engine_port INTEGER NOT NULL DEFAULT 9210,
    standalone_port INTEGER NOT NULL DEFAULT 11437,
    sweep_interval_s INTEGER NOT NULL DEFAULT 60,
    stale_blocker_hours INTEGER NOT NULL DEFAULT 2,
    digest_interval_minutes INTEGER NOT NULL DEFAULT 60,
    max_messages_before_prune INTEGER NOT NULL DEFAULT 5000,
    default_ttl_hours INTEGER NOT NULL DEFAULT 168,
    sweeper_enabled INTEGER NOT NULL DEFAULT 1,
    require_key_for_post INTEGER NOT NULL DEFAULT 0,
    channels_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
