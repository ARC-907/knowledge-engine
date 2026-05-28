"""Knowledge-Engine — Message Board.

Append-only coordination channel for distributed workers. Durable, poll-friendly,
and filterable by task, product, worker, and message type.

Workers post claims, releases, blockers, status updates, and handoff notices.
Workers poll for messages relevant to them. No ambiguity about who said what.

Depends on `knowledge_engine.foundation.db`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..foundation import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_target_node(msg: dict[str, Any]) -> str | None:
    """Extract the target node from a message body JSON (system commands use 'target')."""
    body = msg.get("body")
    if not body:
        return None
    try:
        parsed = json.loads(body) if isinstance(body, str) else body
        return parsed.get("target") or parsed.get("target_node") or parsed.get("target_node_id")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


def _extract_target_role(msg: dict[str, Any]) -> str | None:
    """Extract the target role from a message body JSON."""
    body = msg.get("body")
    if not body:
        return None
    try:
        parsed = json.loads(body) if isinstance(body, str) else body
        return parsed.get("target_role") or parsed.get("role")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


# ── Post / Read ────────────────────────────────────────────


def post_message(
    message_type: str,
    sender_node_id: str,
    sender_role: str | None = None,
    task_id: str | None = None,
    product_id: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    visibility_scope: str = "all",
    target_node_id: str | None = None,
    target_role: str | None = None,
    requires_ack: bool = False,
    reply_to: str | None = None,
    correlation_id: str | None = None,
    ttl_hours: int = 168,
    channel: str = "ops",
    model_id: str | None = None,
) -> dict[str, Any]:
    """Post a message to the board. Returns the created message."""
    message_id = str(uuid.uuid4())
    now = _now_iso()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    ).isoformat() if ttl_hours > 0 else None

    conn = db.get_connection()
    conn.execute(
        """INSERT INTO messages (
            message_id, channel, task_id, product_id, sender_agent_id, sender_node_id,
            sender_role, model_id, message_type, subject, body, created_at, expires_at,
            visibility_scope, requires_ack, reply_to, correlation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id, channel, task_id, product_id, sender_node_id, sender_node_id,
            sender_role, model_id, message_type, subject, body, now, expires_at,
            visibility_scope, 1 if requires_ack else 0, reply_to, correlation_id,
        ),
    )
    conn.commit()
    return read_message(message_id)  # type: ignore[return-value]


def read_message(message_id: str) -> dict[str, Any] | None:
    """Read a single message by ID."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    return db.dict_from_row(row)


# ── Polling ────────────────────────────────────────────────


def poll_messages(
    since: str | None = None,
    task_id: str | None = None,
    product_id: str | None = None,
    message_type: str | None = None,
    sender_node_id: str | None = None,
    limit: int = 50,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    """Poll messages with optional filters. Returns newest first."""
    conn = db.get_connection()

    clauses = []
    params: list[Any] = []

    if channel:
        clauses.append("channel = ?")
        params.append(channel)

    if since:
        clauses.append("created_at > ?")
        params.append(since)
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if product_id:
        clauses.append("product_id = ?")
        params.append(product_id)
    if message_type:
        clauses.append("message_type = ?")
        params.append(message_type)
    if sender_node_id:
        clauses.append("sender_node_id = ?")
        params.append(sender_node_id)

    where = " AND ".join(clauses) if clauses else "1=1"
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM messages WHERE {where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return db.rows_to_dicts(rows)


def get_relevant_messages(
    node_id: str,
    role: str | None = None,
    current_task_ids: list[str] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get messages relevant to a specific worker.

    Relevance rules: same task_id, same product_id, addressed to role,
    addressed to node, or global.
    """
    all_msgs = poll_messages(since=since, limit=limit * 3)

    relevant = []
    task_set = set(current_task_ids or [])

    for msg in all_msgs:
        if msg.get("visibility_scope") == "all":
            relevant.append(msg)
            continue
        if msg.get("task_id") and msg["task_id"] in task_set:
            relevant.append(msg)
            continue
        if msg.get("visibility_scope") == "node":
            target = _extract_target_node(msg)
            if target == node_id or msg.get("sender_node_id") == node_id:
                relevant.append(msg)
            continue
        if msg.get("visibility_scope") == "role":
            target = _extract_target_role(msg)
            if target == role or msg.get("sender_role") == role:
                relevant.append(msg)
            continue
        if msg.get("message_type") in ("node_health", "policy_notice"):
            relevant.append(msg)
            continue

    return relevant[:limit]


# ── Convenience Posters ──────────────────────────────────────


def post_claim(node_id: str, task_id: str, role: str | None = None) -> dict[str, Any]:
    """Announce a task claim on the board."""
    return post_message(
        message_type="claim", sender_node_id=node_id, sender_role=role,
        task_id=task_id, subject=f"Task {task_id[:8]}... claimed by {node_id}",
        visibility_scope="all",
    )


def post_release(node_id: str, task_id: str, reason: str = "") -> dict[str, Any]:
    """Announce a task release."""
    return post_message(
        message_type="release", sender_node_id=node_id,
        task_id=task_id, subject=f"Task {task_id[:8]}... released",
        body=reason, visibility_scope="all",
    )


def post_blocker(node_id: str, task_id: str, description: str) -> dict[str, Any]:
    """Flag a blocker for human or coordinator attention."""
    return post_message(
        message_type="blocker", sender_node_id=node_id,
        task_id=task_id, subject=f"BLOCKED: {task_id[:8]}...",
        body=description, visibility_scope="all", requires_ack=True,
    )


def post_heartbeat_msg(node_id: str, tasks: list[str]) -> dict[str, Any]:
    """Health message for the board."""
    return post_message(
        message_type="node_health", sender_node_id=node_id,
        subject=f"{node_id} alive, {len(tasks)} tasks",
        body=json.dumps(tasks), visibility_scope="all", ttl_hours=1,
    )


# ── Maintenance ──────────────────────────────────────────


def prune_expired() -> int:
    """Delete messages past their TTL. Returns count deleted."""
    now = _now_iso()
    conn = db.get_connection()
    cursor = conn.execute(
        "DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    conn.commit()
    return cursor.rowcount


def prune_by_count(max_messages: int = 500) -> int:
    """Keep only the newest max_messages messages. Returns count deleted."""
    conn = db.get_connection()
    total = conn.execute("SELECT COUNT(*) as cnt FROM messages").fetchone()["cnt"]
    if total <= max_messages:
        return 0
    cursor = conn.execute(
        "DELETE FROM messages WHERE message_id NOT IN "
        "(SELECT message_id FROM messages ORDER BY created_at DESC LIMIT ?) "
        "AND (requires_ack = 0 OR ack_by IS NOT NULL AND ack_by != '[]')",
        (max_messages,),
    )
    conn.commit()
    return cursor.rowcount


def delete_message(message_id: str) -> bool:
    """Delete a single message by ID. Returns True if deleted."""
    conn = db.get_connection()
    cursor = conn.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
    conn.commit()
    return cursor.rowcount > 0


def delete_messages_bulk(message_ids: list[str]) -> int:
    """Delete multiple messages. Returns count deleted."""
    if not message_ids:
        return 0
    conn = db.get_connection()
    placeholders = ",".join("?" for _ in message_ids)
    cursor = conn.execute(
        f"DELETE FROM messages WHERE message_id IN ({placeholders})",
        message_ids,
    )
    conn.commit()
    return cursor.rowcount


def message_count(message_type: str | None = None, channel: str | None = None) -> int:
    """Count messages, optionally by type and/or channel."""
    conn = db.get_connection()
    clauses = []
    params: list[Any] = []
    if message_type:
        clauses.append("message_type = ?")
        params.append(message_type)
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    where = " AND ".join(clauses) if clauses else "1=1"
    row = conn.execute(f"SELECT COUNT(*) as cnt FROM messages WHERE {where}", params).fetchone()
    return row["cnt"] if row else 0
