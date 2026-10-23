-- 006 chat (optional surface)
--
-- Consumers: none inside the engine. Kept (and always created) for chat
-- front ends built on the pipeline store. chat_personas is never seeded:
-- deployments add their own persona content.
--
-- Part of the pre-versioning baseline: statements must stay idempotent.

CREATE TABLE IF NOT EXISTS chat_conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT,
    model_id TEXT,
    provider TEXT,
    node_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived INTEGER DEFAULT 0,
    system_prompt TEXT,
    context_limit INTEGER DEFAULT 20,
    pinned INTEGER DEFAULT 0,
    persona_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_chatconv_updated ON chat_conversations(updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    chat_msg_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model_id TEXT,
    token_count INTEGER,
    created_at TEXT NOT NULL,
    excluded_from_context INTEGER DEFAULT 0,
    bookmarked INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chatmsg_conv ON chat_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_chatmsg_created ON chat_messages(created_at);

CREATE TABLE IF NOT EXISTS chat_personas (
    persona_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    icon TEXT DEFAULT '',
    system_prompt TEXT NOT NULL,
    suggested_starters TEXT DEFAULT '[]',
    sort_order INTEGER DEFAULT 0,
    is_default INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_prompt_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    variables TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
