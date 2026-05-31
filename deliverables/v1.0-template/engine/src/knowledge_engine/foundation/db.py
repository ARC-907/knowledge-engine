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

import contextlib
import contextvars
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config  # noqa: F401  — ensures .env is loaded before DB path resolves

# ── DB path resolution ────────────────────────────────────────────────
# Resolved DYNAMICALLY on every connection request, not frozen at import.
#
# A coordination backbone that bakes its database path into a module-level
# constant the moment it is first imported cannot be "hoisted into any
# system": it breaks the instant the host reconfigures `KE_PIPELINE_DB` at
# runtime, runs two engines in one process, or re-imports the package. The
# symptom is a worker thread silently reading a stale database while the
# main thread writes to the new one — data that looks lost but isn't.
#
# `resolve_db_path()` reads the environment on each call; `get_connection`
# caches the opened connection per-resolved-path in thread-local storage,
# so the dynamic read costs one `os.environ.get` and changing the target
# DB at runtime Just Works (each path gets its own cached connection).
#
# A `contextvars.ContextVar` ("scope DB") lets a caller route a whole block
# of work — every nested get_connection, on the same thread or an awaited
# task — to a *different* physical database without threading a `db_path`
# argument through every function. This is what powers per-project /
# per-branch / per-agent database segregation: set the scope, and the
# board, queue, key vault, and sweeper underneath all follow it. Precedence
# is explicit-arg → scope-context → env → default, so an explicit path
# always wins and the context only fills the "no arg" case.

_scope_db_path: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ke_scope_db_path", default=None
)


def resolve_db_path(db_path: Path | str | None = None) -> str:
    """Resolve the active pipeline DB path. Read on every connection request.

    Precedence: explicit ``db_path`` arg → active scope context
    (``using_db``) → ``KE_PIPELINE_DB`` env → ``KE_DATA_DIR``/pipeline.db →
    ``./engine/data/pipeline.db``.
    """
    if db_path:
        return str(Path(db_path).resolve())
    scoped = _scope_db_path.get()
    if scoped:
        return scoped
    env_db = os.environ.get("KE_PIPELINE_DB")
    if env_db:
        return str(Path(env_db).resolve())
    data_dir = os.environ.get("KE_DATA_DIR")
    base = Path(data_dir) if data_dir else (Path.cwd() / "engine" / "data")
    return str((base / "pipeline.db").resolve())


@contextlib.contextmanager
def using_db(db_path: Path | str) -> Iterator[str]:
    """Route every no-arg ``get_connection()`` in this block to ``db_path``.

    Resolves + creates the target DB (schema-initialized on first connect),
    binds it to the scope ContextVar for the duration of the block, and
    restores the prior scope on exit. Nestable. Yields the resolved path.

    Used by the agent board to give each project / branch / agent / loop
    its own physical SQLite engine-block while sharing one process::

        with using_db(scope_db_path("branch-feat-auth")):
            store.post_with_validation(...)   # lands in the scoped DB
    """
    resolved = str(Path(db_path).resolve())
    token = _scope_db_path.set(resolved)
    try:
        yield resolved
    finally:
        _scope_db_path.reset(token)


def current_db_path() -> str:
    """The DB path that a no-arg ``get_connection()`` would use right now."""
    return resolve_db_path()


def active_scope_db_path() -> str | None:
    """The scope-context DB path in effect, or None if unscoped."""
    return _scope_db_path.get()


# Back-compat module attribute. Reflects the import-time default; callers
# that need the live value must use ``resolve_db_path()`` / ``current_db_path()``
# (``get_connection`` already does). Kept so existing references don't break.
DB_PATH = Path(resolve_db_path()).resolve()

_local = threading.local()

# Per-process guard so the one-shot FTS5 backfill runs exactly once per DB
# path. _init_schema is called on every new thread-local connection; without
# this set, every worker thread would re-scan messages on first use.
_FTS_BACKFILLED: set[str] = set()
_FTS_BACKFILLED_LOCK = threading.Lock()

# Serializes ensure_master_key against concurrent bootstrap calls. The
# unique partial index in the schema is the authoritative defense; this
# lock just avoids the duplicate-key error path under normal racing.
_MASTER_KEY_LOCK = threading.Lock()

# JSON-serialized fields — auto-deserialized by dict_from_row
_JSON_FIELDS = frozenset({
    "depends_on", "assigned_capability", "source_policy",
    "output_artifacts", "audit_trail", "capabilities",
    "current_tasks", "config_json", "ack_by", "available_models",
    "tags", "shared_with", "input_schema", "output_schema",
    "input_json", "meta_json",
})


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local database connection. Creates DB and schema if needed.

    The path is resolved dynamically (``resolve_db_path``) so a host that
    reconfigures ``KE_PIPELINE_DB`` at runtime — or runs more than one DB in
    a single process — gets the right connection. Connections are cached
    per-resolved-path in thread-local storage, so the dynamic read is cheap.
    """
    path = resolve_db_path(db_path)

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

        -- ── Agent Board ─────────────────────────────────────────────
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
    """)
    # FTS5 virtual table + triggers — created outside the executescript so we
    # can swallow the "no fts5 support" error on builds without the extension
    # and still let the rest of the schema initialize. The board falls back to
    # LIKE search if FTS5 is unavailable.
    #
    # Standard (non-contentless) FTS5 — stores its own copy of subject + body
    # so snippet() and bm25() work without the messages table being co-located.
    # Bodies are bounded to ~50KB by the post validator, so the extra storage
    # is small.
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                message_id UNINDEXED,
                channel,
                subject,
                body
            );

            CREATE TRIGGER IF NOT EXISTS messages_fts_ai
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(message_id, channel, subject, body)
                VALUES (new.message_id, new.channel, COALESCE(new.subject,''), COALESCE(new.body,''));
            END;

            CREATE TRIGGER IF NOT EXISTS messages_fts_ad
            AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE message_id = old.message_id;
            END;

            CREATE TRIGGER IF NOT EXISTS messages_fts_au
            AFTER UPDATE OF subject, body, channel ON messages BEGIN
                DELETE FROM messages_fts WHERE message_id = old.message_id;
                INSERT INTO messages_fts(message_id, channel, subject, body)
                VALUES (new.message_id, new.channel, COALESCE(new.subject,''), COALESCE(new.body,''));
            END;
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from an older schema. Idempotent."""
    _add_column(conn, "messages", "model_id", "TEXT")
    _add_column(conn, "messages", "thread_id", "TEXT")
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
    seed_board_config(conn)
    _backfill_messages_fts_once(conn)


def seed_board_config(conn: sqlite3.Connection) -> None:
    """Insert the singleton board_config row on first boot. Idempotent.

    Public so other modules can rebuild the config from defaults without
    reaching into private names.
    """
    row = conn.execute("SELECT id FROM board_config WHERE id = 1").fetchone()
    if row is not None:
        return
    now = datetime.now(timezone.utc).isoformat()
    default_channels = json.dumps([
        "ops", "research", "project", "worktree", "branch",
        "library", "planning", "execution", "testing", "chatter",
    ])
    conn.execute(
        """INSERT INTO board_config (
            id, engine_port, standalone_port, sweep_interval_s,
            stale_blocker_hours, digest_interval_minutes,
            max_messages_before_prune, default_ttl_hours,
            sweeper_enabled, require_key_for_post, channels_json, updated_at
        ) VALUES (1, 9210, 11437, 60, 2, 60, 5000, 168, 1, 0, ?, ?)""",
        (default_channels, now),
    )
    conn.commit()


def _backfill_messages_fts_once(conn: sqlite3.Connection) -> None:
    """Mirror any pre-FTS messages into messages_fts. Runs exactly once per
    DB path per process so a many-thread server doesn't pay the LEFT JOIN
    cost on every new worker-thread connection.

    Silently no-ops if FTS5 isn't compiled in or the FTS table is missing.
    """
    path = _conn_path(conn)
    with _FTS_BACKFILLED_LOCK:
        if path in _FTS_BACKFILLED:
            return
        _FTS_BACKFILLED.add(path)
    try:
        rows = conn.execute(
            """SELECT m.message_id, m.channel,
                      COALESCE(m.subject,'') AS subject,
                      COALESCE(m.body,'')    AS body
               FROM messages m
               LEFT JOIN messages_fts f ON f.message_id = m.message_id
               WHERE f.message_id IS NULL"""
        ).fetchall()
        if not rows:
            return
        conn.executemany(
            "INSERT INTO messages_fts(message_id, channel, subject, body) VALUES (?, ?, ?, ?)",
            [(r["message_id"], r["channel"], r["subject"], r["body"]) for r in rows],
        )
        conn.commit()
    except sqlite3.OperationalError:
        # FTS5 missing or table corrupt — leave it; search falls back to LIKE.
        pass


def _conn_path(conn: sqlite3.Connection) -> str:
    """Return the file path backing this connection (for caching keys)."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row:
            d = dict(row) if hasattr(row, "keys") else {}
            for key in ("file", "name", "seq"):
                if key in d and d[key]:
                    return str(d[key])
    except sqlite3.Error:
        pass
    return resolve_db_path()


# ── Sweeper lease (board) ────────────────────────────────────────────


def acquire_sweeper_lease(holder: str, ttl_seconds: int = 120) -> bool:
    """Try to claim the singleton sweeper lease in kv_store.

    Returns True if the caller now owns the lease; False if another holder
    has a valid lease. Lets the embedded-FastAPI sweeper and a standalone
    sweeper coexist on the same DB without doubling reminders / digests.

    The lease is a JSON value `{holder, expires_at}` keyed by the literal
    string "board.sweeper_lease". Steals expired leases.
    """
    if ttl_seconds < 5:
        ttl_seconds = 5
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    payload = json.dumps({"holder": holder, "expires_at": expires_at})

    conn = get_connection()
    # SQLite needs serialization across writers; busy_timeout already set.
    cursor = conn.execute(
        "SELECT value FROM kv_store WHERE key = 'board.sweeper_lease'"
    )
    row = cursor.fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO kv_store(key, value, updated_at) VALUES (?, ?, ?)",
                ("board.sweeper_lease", payload, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Lost the insert race — fall through to UPDATE path.
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = 'board.sweeper_lease'"
            ).fetchone()

    # Decide whether to steal.
    if row is not None:
        try:
            current = json.loads(row["value"])
            cur_expires = current.get("expires_at", "")
        except (json.JSONDecodeError, TypeError, KeyError):
            cur_expires = ""
        if current.get("holder") == holder and cur_expires > now:
            # Already ours; renew.
            pass
        elif cur_expires and cur_expires > now:
            return False  # Someone else holds a live lease.

    # Renew or steal.
    conn.execute(
        "UPDATE kv_store SET value = ?, updated_at = ? WHERE key = 'board.sweeper_lease'",
        (payload, now),
    )
    conn.commit()
    return True


def release_sweeper_lease(holder: str) -> None:
    """Release the lease if this holder owns it. Best-effort."""
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM kv_store WHERE key = 'board.sweeper_lease'"
    ).fetchone()
    if row is None:
        return
    try:
        current = json.loads(row["value"])
        if current.get("holder") != holder:
            return
    except (json.JSONDecodeError, TypeError):
        return
    conn.execute("DELETE FROM kv_store WHERE key = 'board.sweeper_lease'")
    conn.commit()


def master_key_lock() -> threading.Lock:
    """Module-level lock used by the board's master-key bootstrap to
    serialize concurrent calls before they touch the DB. The schema's
    unique partial index on `is_master` is the authoritative defense.
    """
    return _MASTER_KEY_LOCK


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
