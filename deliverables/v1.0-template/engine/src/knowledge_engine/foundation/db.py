"""Knowledge-Engine — Pipeline SQLite backbone.

Single database file provides durable state for queue, registry, message board,
tools, and chat. WAL mode for concurrent reads. Schema auto-creates on first
connection.

Env vars:
    KE_PIPELINE_DB     (default: $KE_DATA_DIR/pipeline.db or ./engine/data/pipeline.db)

This is a coordination store, not a document store. Research objects, source
records, and handoff bundles remain on the filesystem for inspectability.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config  # noqa: F401  — ensures .env is loaded before DB path resolves

# ── Resolve DB path from env vars ─────────────────────────────────────
_default_db_dir = Path(os.environ.get("KE_DATA_DIR", str(Path.cwd() / "engine" / "data")))
DB_PATH = Path(
    os.environ.get("KE_PIPELINE_DB", str(_default_db_dir / "pipeline.db"))
).resolve()

_local = threading.local()

# JSON-serialized fields — auto-deserialized by dict_from_row
_JSON_FIELDS = frozenset({
    "depends_on", "assigned_capability", "source_policy",
    "output_artifacts", "audit_trail", "capabilities",
    "current_tasks", "config_json", "ack_by", "available_models",
    "tags", "shared_with", "input_schema", "output_schema",
    "input_json", "meta_json",
})


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local database connection. Creates DB and schema if needed."""
    path = str(db_path) if db_path else str(DB_PATH)

    if not hasattr(_local, "connections"):
        _local.connections = {}

    if path not in _local.connections:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(conn)
        _local.connections[path] = conn

    return _local.connections[path]


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
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

        CREATE TABLE IF NOT EXISTS api_keys (
            key_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            display_name TEXT NOT NULL,
            env_var TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            last_verified TEXT,
            notes TEXT
        );

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

        -- ── Tool/Script Hosting ──────────────────────────────────
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
            invocation_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tools_kind ON hosted_tools(kind);
        CREATE INDEX IF NOT EXISTS idx_tools_route ON hosted_tools(route);

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

        -- ── Chat (optional surface) ───────────────────────────────
        CREATE TABLE IF NOT EXISTS chat_conversations (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            model_id TEXT,
            provider TEXT,
            node_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_chatconv_updated ON chat_conversations(updated_at);

        CREATE TABLE IF NOT EXISTS chat_messages (
            chat_msg_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model_id TEXT,
            token_count INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chatmsg_conv ON chat_messages(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_chatmsg_created ON chat_messages(created_at);

        -- ── Agent API Keys (auth for tool routes / MCP) ───────────
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

        -- ── Chat Personas (table only; not seeded) ────────────────
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

        -- ── Pipeline Runs (multi-stage task sequences) ──────────
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

        -- Key-value store for sync state and misc config
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from an older schema. Idempotent."""
    _add_column(conn, "messages", "model_id", "TEXT")
    _add_column(conn, "workers", "safeguard_locked", "INTEGER DEFAULT 0")
    _add_column(conn, "workers", "safeguard_reason", "TEXT")
    _add_column(conn, "workers", "resources_json", "TEXT DEFAULT '{}'")
    _add_column(conn, "tasks", "input_json", "TEXT NOT NULL DEFAULT '{}'")
    _add_column(conn, "tasks", "meta_json", "TEXT")
    _add_column(conn, "chat_conversations", "system_prompt", "TEXT")
    _add_column(conn, "chat_conversations", "context_limit", "INTEGER DEFAULT 20")
    _add_column(conn, "chat_conversations", "pinned", "INTEGER DEFAULT 0")
    _add_column(conn, "chat_conversations", "persona_id", "TEXT")
    _add_column(conn, "chat_messages", "excluded_from_context", "INTEGER DEFAULT 0")
    _add_column(conn, "chat_messages", "bookmarked", "INTEGER DEFAULT 0")
    conn.commit()


def _add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def dict_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a sqlite3.Row to a dict, deserializing JSON fields."""
    if row is None:
        return None
    d = dict(row)
    for field in _JSON_FIELDS:
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert multiple rows to dicts."""
    return [d for r in rows if r is not None if (d := dict_from_row(r)) is not None]


def log_event(
    event_type: str,
    task_id: str | None = None,
    node_id: str | None = None,
    agent_id: str | None = None,
    detail: str | None = None,
    data: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Append an event to the audit log."""
    if conn is None:
        conn = get_connection()
    conn.execute(
        """INSERT INTO events (timestamp, event_type, task_id, node_id, agent_id, detail, data_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            event_type, task_id, node_id, agent_id, detail,
            json.dumps(data) if data else None,
        ),
    )
    conn.commit()


def close_all() -> None:
    """Close all thread-local connections."""
    if hasattr(_local, "connections"):
        for conn in _local.connections.values():
            try:
                conn.close()
            except Exception:
                pass
        _local.connections.clear()
