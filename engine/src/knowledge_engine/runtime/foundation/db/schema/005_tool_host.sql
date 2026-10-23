-- 005 tool / script hosting
--
-- Consumers: tool_host.host (register / invoke / proxy / serve hosted
-- tools) and the `knowledge-engine` corpus CLI inventory. NULL
-- allowed_methods / allowed_headers mean "use the host defaults".
--
-- Part of the pre-versioning baseline: statements must stay idempotent.

CREATE TABLE IF NOT EXISTS hosted_tools (
    tool_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    description TEXT,
    route TEXT NOT NULL UNIQUE,
    input_schema TEXT DEFAULT '{}',
    output_schema TEXT DEFAULT '{}',
    command TEXT,
    working_dir TEXT,
    timeout_seconds INTEGER DEFAULT 30,
    upstream_url TEXT,
    health_endpoint TEXT,
    local_path TEXT,
    node_id TEXT,
    enabled INTEGER DEFAULT 1,
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_invoked_at TEXT,
    invocation_count INTEGER DEFAULT 0,
    allow_inline INTEGER DEFAULT 0,
    allowed_methods TEXT,
    allowed_headers TEXT
);
CREATE INDEX IF NOT EXISTS idx_tools_kind ON hosted_tools(kind);
CREATE INDEX IF NOT EXISTS idx_tools_route ON hosted_tools(route);
