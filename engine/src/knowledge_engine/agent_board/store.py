"""Agent Board — store facade.

Sits between the schema layer and the existing `pipeline/message_board.py`
primitives. Adds:

* Schema-validated posting (`post_with_validation`)
* FTS5 search with bm25 ranking + snippets (`search_messages`)
* Thread traversal by `correlation_id` (`thread_messages`)
* Context-compressed digest for LLM callers (`digest`)
* Stale-blocker discovery for the sweeper (`get_unacked_blockers`)
* Acknowledgement helper (`ack_message`)
* Per-channel and per-type stats for the dashboard (`channel_stats`)
* Runtime config read/write (`load_config`, `update_config`)

All functions return plain dicts — no ORM, no models. Cheap to serialize.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from ..runtime.foundation import db
from ..runtime.pipeline import message_board as mb
from . import schemas
from .scopes import scope_db_path, scope_exists

_logger = logging.getLogger(__name__)


class _Scoped[R](Protocol):
    """Call signature of a store function after ``_scoped`` wrapping."""

    def __call__(self, *args: Any, scope: str | None = None, **kwargs: Any) -> R: ...


# Store functions allowed to bring a scope database into existence. Every
# other scoped function is a read (or a maintenance pass) and must answer
# "empty" for a scope that has no database yet — otherwise any trusted
# ``GET ...?scope=<new>`` would mint a fresh SQLite file on disk.
_SCOPE_CREATORS: frozenset[str] = frozenset({"post_with_validation", "update_config"})


def _default_config() -> dict[str, Any]:
    cfg = db.default_board_config()
    cfg["channels"] = list(db.BOARD_CONFIG_DEFAULT_CHANNELS)
    return cfg


def _empty_digest(
    channel: str | None = None,
    since: str | None = None,
    max_messages: int = 200,
    include_sweeper_posts: bool = False,
) -> dict[str, Any]:
    return {
        "channel": channel or "(all)",
        "since": since,
        "scanned": 0,
        "counts_by_type": {},
        "most_recent_per_type": {},
        "open_blockers": [],
        "top_senders": [],
        "busy_threads": [],
    }


# What each read returns for a scope that does not exist. Keyed by function
# name; the factory receives the call's own arguments so shape-dependent
# answers (``digest``) can echo the filters back like the real thing.
_EMPTY_FOR_UNKNOWN_SCOPE: dict[str, Callable[..., Any]] = {
    "load_config": lambda *a, **k: _default_config(),
    "read": lambda *a, **k: None,
    "poll": lambda *a, **k: [],
    "relevant_for": lambda *a, **k: [],
    "thread_messages": lambda *a, **k: [],
    "search_messages": lambda *a, **k: [],
    "digest": _empty_digest,
    "get_unacked_blockers": lambda *a, **k: [],
    "ack_message": lambda *a, **k: None,
    "channel_stats": lambda *a, **k: [],
    "type_stats": lambda *a, **k: [],
    "total_count": lambda *a, **k: 0,
    "prune_expired": lambda *a, **k: 0,
    "prune_by_count": lambda *a, **k: 0,
}


def _scoped[R](fn: Callable[..., R]) -> _Scoped[R]:
    """Give a store function an optional keyword-only ``scope=`` argument.

    When ``scope`` is supplied, the call runs inside
    ``foundation.db.using_db(scope_db_path(scope))`` so every nested
    ``get_connection()`` — including the ones inside ``pipeline.message_board``
    and the key vault — routes to that scope's physical database. When
    ``scope`` is omitted the call is a straight passthrough that inherits
    whatever scope context is already active (so a wrapped function calling
    another wrapped function does the right thing without re-routing).

    Only functions in ``_SCOPE_CREATORS`` may create the scope database; a
    read of a scope that has none returns its empty value without touching
    the filesystem. A malformed scope name raises ``ValueError`` either way.

    The wrapping is applied once at module load (see the loop near the
    bottom of this file) so each function body stays scope-agnostic.
    """
    creates = fn.__name__ in _SCOPE_CREATORS
    empty = _EMPTY_FOR_UNKNOWN_SCOPE.get(fn.__name__)

    @functools.wraps(fn)
    def wrapper(*args: Any, scope: str | None = None, **kwargs: Any) -> Any:
        if not scope:
            return fn(*args, **kwargs)
        if not creates and empty is not None and not scope_exists(scope):
            _logger.debug("%s: scope %r has no database; returning empty", fn.__name__, scope)
            return empty(*args, **kwargs)
        with db.using_db(scope_db_path(scope, create=creates)):
            return fn(*args, **kwargs)

    return wrapper


if TYPE_CHECKING:
    # Static mirror of the wrapping loop at the bottom of this file: the loop
    # rebinds names through ``globals()``, which type checkers cannot follow, so
    # each function in ``_SCOPED_FUNCTIONS`` is marked with this decorator to give
    # callers the ``scope=`` keyword. It is a no-op at runtime.
    def _accepts_scope[R](fn: Callable[..., R]) -> _Scoped[R]: ...

else:

    def _accepts_scope(fn):
        return fn


# ── Config (singleton row) ─────────────────────────────────────


@_accepts_scope
def load_config() -> dict[str, Any]:
    """Return the current board_config singleton as a dict."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM board_config WHERE id = 1").fetchone()
    if row is None:
        # Defensive — seed_board_config should have run during _migrate;
        # reseed if a downstream caller wiped the row.
        db.seed_board_config(conn)
        row = conn.execute("SELECT * FROM board_config WHERE id = 1").fetchone()
    cfg = dict(row)
    try:
        cfg["channels"] = json.loads(cfg.get("channels_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        cfg["channels"] = list(schemas.DEFAULT_CHANNELS)
    return cfg


_CONFIG_INT_FIELDS = frozenset(
    {
        "engine_port",
        "standalone_port",
        "sweep_interval_s",
        "stale_blocker_hours",
        "digest_interval_minutes",
        "max_messages_before_prune",
        "default_ttl_hours",
        "sweeper_enabled",
        "require_key_for_post",
    }
)


@_accepts_scope
def update_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update to board_config. Returns the new state."""
    if not patch:
        return load_config()
    conn = db.get_connection()
    sets: list[str] = []
    params: list[Any] = []
    for key, value in patch.items():
        if key == "channels":
            sets.append("channels_json = ?")
            params.append(json.dumps(list(value)))
            continue
        if key in _CONFIG_INT_FIELDS:
            sets.append(f"{key} = ?")
            params.append(int(value))
            continue
        # Skip unknown keys silently — UI is the source of truth for shape.
    if not sets:
        return load_config()
    sets.append("updated_at = ?")
    params.append(_now_iso())
    conn.execute(
        f"UPDATE board_config SET {', '.join(sets)} WHERE id = 1",
        params,
    )
    conn.commit()
    return load_config()


# ── Post / Read facades ────────────────────────────────────────


@_accepts_scope
def post_with_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """Schema-validate then post. Returns the created message or raises.

    Raises `ValueError` with all collected errors joined on '; '.
    """
    cfg = load_config()
    draft, errors = schemas.validate(payload, known_channels=cfg["channels"])
    if errors or draft is None:
        raise ValueError("; ".join(errors))
    msg = mb.post_message(
        message_type=draft.message_type,
        sender_node_id=draft.sender_node_id,
        sender_role=draft.sender_role,
        task_id=draft.task_id,
        product_id=draft.product_id,
        subject=draft.subject,
        body=draft.body,
        visibility_scope=draft.visibility_scope,
        target_node_id=draft.target_node_id,
        target_role=draft.target_role,
        requires_ack=draft.requires_ack,
        reply_to=draft.reply_to,
        correlation_id=draft.correlation_id,
        ttl_hours=draft.ttl_hours,
        channel=draft.channel,
        model_id=draft.model_id,
        thread_id=draft.thread_id,
    )
    return msg or {}


@_accepts_scope
def read(message_id: str) -> dict[str, Any] | None:
    return mb.read_message(message_id)


@_accepts_scope
def poll(
    since: str | None = None,
    channel: str | None = None,
    message_type: str | None = None,
    task_id: str | None = None,
    product_id: str | None = None,
    sender_node_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return mb.poll_messages(
        since=since,
        task_id=task_id,
        product_id=product_id,
        message_type=message_type,
        sender_node_id=sender_node_id,
        limit=limit,
        channel=channel,
    )


@_accepts_scope
def relevant_for(
    node_id: str,
    role: str | None = None,
    current_task_ids: list[str] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return mb.get_relevant_messages(
        node_id=node_id,
        role=role,
        current_task_ids=current_task_ids,
        since=since,
        limit=limit,
    )


# ── Threading ──────────────────────────────────────────────────


@_accepts_scope
def thread_messages(
    correlation_id: str | None = None,
    thread_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a thread's messages, oldest-first for natural reading order.

    Either `correlation_id` or `thread_id` is required (correlation_id wins
    if both are passed). Replies are also pulled in via `reply_to` chasing.
    """
    if not correlation_id and not thread_id:
        return []
    conn = db.get_connection()
    if correlation_id:
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE correlation_id = ?
                  OR message_id = ?
                  OR reply_to = ?
               ORDER BY created_at ASC LIMIT ?""",
            (correlation_id, correlation_id, correlation_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM messages WHERE thread_id = ?
               ORDER BY created_at ASC LIMIT ?""",
            (thread_id, limit),
        ).fetchall()
    return db.rows_to_dicts(rows)


# ── FTS5 search ────────────────────────────────────────────────


def _fts5_quote(query: str) -> str:
    """Wrap a raw user query in FTS5 phrase quotes, escaping internal quotes.

    Example: ``foo (bar)`` becomes ``"foo (bar)"`` (a literal phrase with no
    FTS5 syntax error). Any double-quote inside the query is doubled per the
    FTS5 phrase-escape rule.
    """
    return '"' + query.replace('"', '""') + '"'


@_accepts_scope
def search_messages(
    query: str,
    channel: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search subject+body via FTS5 with bm25 ranking and snippet highlighting.

    Resilient to user input that happens to collide with FTS5 query syntax:
    a raw query like `foo (bar)` would otherwise raise `sqlite3.OperationalError`
    on parse. The two-stage retry preserves power-user behaviour:

    1. Try the query as-is. Power users keep operators like `*`, `AND`, `OR`,
       `NEAR()`, column filters.
    2. If FTS5 rejects the query, re-run wrapped as a phrase
       (`"foo (bar)"`) so the dashboard search box can never 500 on a
       parenthesis or stray punctuation.
    3. If even the quoted form fails (FTS5 truly unavailable), fall back to
       a LIKE scan over `subject`/`body`.

    Snippets are plain-text — no HTML — so MCP and dashboard callers can
    render directly.
    """
    if not query.strip():
        return []
    conn = db.get_connection()
    sql = """
        SELECT m.*,
               snippet(messages_fts, -1, '[', ']', '…', 16) AS snippet,
               bm25(messages_fts) AS rank_score
        FROM messages_fts
        JOIN messages m ON m.message_id = messages_fts.message_id
        WHERE messages_fts MATCH ?
    """
    if channel:
        sql += " AND m.channel = ?"
    sql += " ORDER BY rank_score LIMIT ?"

    attempts: list[str] = [query]
    quoted = _fts5_quote(query)
    if quoted != query:
        attempts.append(quoted)

    last_error: sqlite3.OperationalError | None = None
    for attempt in attempts:
        params: list[Any] = [attempt]
        if channel:
            params.append(channel)
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
            return db.rows_to_dicts(rows)
        except sqlite3.OperationalError as exc:
            last_error = exc
            continue

    # FTS5 missing or both attempts failed — fall back to LIKE scan.
    _ = last_error  # captured for debugging; intentionally unused
    return _like_search(conn, query, channel, limit)


def _like_search(
    conn: sqlite3.Connection,
    query: str,
    channel: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    pattern = f"%{query}%"
    sql = """
        SELECT *, NULL AS snippet, NULL AS rank_score
        FROM messages
        WHERE (subject LIKE ? OR body LIKE ?)
    """
    params: list[Any] = [pattern, pattern]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return db.rows_to_dicts(rows)


# ── Context-compressed digest ──────────────────────────────────


_SWEEPER_NODE_ID = "board-sweeper"
_DIGEST_SELF_TYPES = frozenset({"digest"})


@_accepts_scope
def digest(
    channel: str | None = None,
    since: str | None = None,
    max_messages: int = 200,
    include_sweeper_posts: bool = False,
) -> dict[str, Any]:
    """Compressed summary of recent activity. The MCP context-saver.

    Instead of returning N full message bodies (cheap to flood a context
    window), returns a short structured summary:

    * Counts by message_type
    * Most-recent message per type (subject only)
    * Open blockers (requires_ack=1 AND not yet acked)
    * Active senders (top 5 by post count)
    * Threads with >= 3 messages (correlation_id + count)

    Sweeper-emitted `digest` posts are excluded by default — otherwise
    each new digest would count prior digests in its `scanned` set,
    making `top_senders` collapse to `board-sweeper`. Set
    `include_sweeper_posts=True` if you actually want the full picture.

    Callers that need full bodies should call `poll` or `thread_messages`.
    """
    msgs = poll(since=since, channel=channel, limit=max_messages)
    if not include_sweeper_posts:
        msgs = [
            m
            for m in msgs
            if not (
                m.get("sender_node_id") == _SWEEPER_NODE_ID
                and m.get("message_type") in _DIGEST_SELF_TYPES
            )
        ]
    by_type: dict[str, int] = {}
    by_sender: dict[str, int] = {}
    by_thread: dict[str, int] = {}
    most_recent_per_type: dict[str, dict[str, Any]] = {}
    open_blockers: list[dict[str, Any]] = []

    for msg in msgs:
        mt = msg.get("message_type") or "unknown"
        by_type[mt] = by_type.get(mt, 0) + 1
        if mt not in most_recent_per_type:
            most_recent_per_type[mt] = {
                "message_id": msg.get("message_id"),
                "subject": msg.get("subject"),
                "from": msg.get("sender_node_id"),
                "at": msg.get("created_at"),
            }
        sender = msg.get("sender_node_id") or "unknown"
        by_sender[sender] = by_sender.get(sender, 0) + 1
        corr = msg.get("correlation_id")
        if corr:
            by_thread[corr] = by_thread.get(corr, 0) + 1
        if msg.get("requires_ack") and not _has_ack(msg):
            open_blockers.append(
                {
                    "message_id": msg.get("message_id"),
                    "subject": msg.get("subject"),
                    "task_id": msg.get("task_id"),
                    "from": msg.get("sender_node_id"),
                    "at": msg.get("created_at"),
                }
            )

    top_senders = sorted(by_sender.items(), key=lambda kv: kv[1], reverse=True)[:5]
    busy_threads = [
        {"correlation_id": cid, "count": cnt} for cid, cnt in by_thread.items() if cnt >= 3
    ]

    return {
        "channel": channel or "(all)",
        "since": since,
        "scanned": len(msgs),
        "counts_by_type": by_type,
        "most_recent_per_type": most_recent_per_type,
        "open_blockers": open_blockers,
        "top_senders": [{"from": s, "count": c} for s, c in top_senders],
        "busy_threads": busy_threads,
    }


# ── Sweeper inputs ─────────────────────────────────────────────


@_accepts_scope
def get_unacked_blockers(threshold_hours: int = 2) -> list[dict[str, Any]]:
    """Blockers that are requires_ack=1, unacked, and older than threshold."""
    cutoff = (datetime.now(UTC) - timedelta(hours=threshold_hours)).isoformat()
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE message_type = 'blocker'
             AND requires_ack = 1
             AND created_at < ?
             AND (ack_by IS NULL OR ack_by IN ('', '[]'))""",
        (cutoff,),
    ).fetchall()
    return db.rows_to_dicts(rows)


@_accepts_scope
def ack_message(message_id: str, acker: str) -> dict[str, Any] | None:
    """Append `acker` to ack_by JSON list. Returns the updated message.

    Race-safe: the append happens inside a `BEGIN IMMEDIATE` transaction
    and uses SQLite's json1 functions so two concurrent acks from
    different threads / clients can't clobber each other. If two callers
    pass the same `acker`, the result is still a list containing that
    acker exactly once.
    """
    conn = db.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ack_by FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        raw = row["ack_by"] or "[]"
        try:
            ack_list = json.loads(raw)
            if not isinstance(ack_list, list):
                ack_list = []
        except (json.JSONDecodeError, TypeError):
            ack_list = []
        if acker not in ack_list:
            ack_list.append(acker)
        conn.execute(
            "UPDATE messages SET ack_by = ? WHERE message_id = ?",
            (json.dumps(ack_list), message_id),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    return mb.read_message(message_id)


# ── Stats ──────────────────────────────────────────────────────


@_accepts_scope
def channel_stats() -> list[dict[str, Any]]:
    """Per-channel counts for the dashboard."""
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT channel,
                  COUNT(*) AS total,
                  SUM(CASE WHEN requires_ack = 1
                            AND (ack_by IS NULL OR ack_by IN ('', '[]'))
                           THEN 1 ELSE 0 END) AS open_blockers,
                  MAX(created_at) AS last_post_at
           FROM messages
           GROUP BY channel
           ORDER BY total DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


@_accepts_scope
def type_stats(channel: str | None = None) -> list[dict[str, Any]]:
    """Per-message-type counts, optionally scoped to a channel."""
    conn = db.get_connection()
    if channel:
        rows = conn.execute(
            """SELECT message_type, COUNT(*) AS total
               FROM messages WHERE channel = ?
               GROUP BY message_type ORDER BY total DESC""",
            (channel,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT message_type, COUNT(*) AS total
               FROM messages GROUP BY message_type ORDER BY total DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@_accepts_scope
def total_count() -> int:
    return mb.message_count()


# ── Maintenance passthroughs ───────────────────────────────────


@_accepts_scope
def prune_expired() -> int:
    return mb.prune_expired()


@_accepts_scope
def prune_by_count(max_messages: int) -> int:
    return mb.prune_by_count(max_messages)


def record_sweep(
    started_at: str,
    finished_at: str,
    pruned_expired: int,
    pruned_overflow: int,
    reminders_emitted: int,
    digests_emitted: int,
    error: str | None = None,
) -> None:
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO board_sweeps (
            started_at, finished_at,
            pruned_expired, pruned_overflow,
            reminders_emitted, digests_emitted, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            started_at,
            finished_at,
            pruned_expired,
            pruned_overflow,
            reminders_emitted,
            digests_emitted,
            error,
        ),
    )
    conn.commit()


def last_sweep() -> dict[str, Any] | None:
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM board_sweeps ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ── Helpers ────────────────────────────────────────────────────


def _has_ack(msg: dict[str, Any]) -> bool:
    ack = msg.get("ack_by")
    if isinstance(ack, list):
        return bool(ack)
    if isinstance(ack, str):
        return ack not in ("", "[]")
    return False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Scope wrapping ─────────────────────────────────────────────
# Apply @_scoped to every public read/write/config function so each gains
# an optional keyword-only `scope=`. Done in one place so the bodies above
# stay scope-agnostic and internal cross-calls (digest→poll, post→load_config)
# inherit the active scope context instead of re-routing. Pure-maintenance
# helpers used only by the sweeper (`record_sweep`, `last_sweep`) are left
# unwrapped — the sweeper sets the scope context itself. Keep this tuple in
# sync with the ``@_accepts_scope`` markers above (typing only).
_SCOPED_FUNCTIONS = (
    "load_config",
    "update_config",
    "post_with_validation",
    "read",
    "poll",
    "relevant_for",
    "thread_messages",
    "search_messages",
    "digest",
    "get_unacked_blockers",
    "ack_message",
    "channel_stats",
    "type_stats",
    "total_count",
    "prune_expired",
    "prune_by_count",
)
for _name in _SCOPED_FUNCTIONS:
    globals()[_name] = _scoped(globals()[_name])
del _name


# Convenience re-exports for callers that want flat imports.
__all__ = [
    "ack_message",
    "channel_stats",
    "digest",
    "get_unacked_blockers",
    "last_sweep",
    "load_config",
    "poll",
    "post_with_validation",
    "prune_by_count",
    "prune_expired",
    "read",
    "record_sweep",
    "relevant_for",
    "search_messages",
    "thread_messages",
    "total_count",
    "type_stats",
    "update_config",
]
