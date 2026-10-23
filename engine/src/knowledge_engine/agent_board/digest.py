"""Agent Board -- context-compressed digest analytics.

Pure functions over already-fetched message dicts: no database access, no
scope handling. :func:`knowledge_engine.agent_board.store.digest` polls the
messages (scope-aware) and hands them to :func:`summarize`; the sweeper's
periodic digest posts and the MCP / HTTP digest surfaces all share it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

#: The sweeper's sender id; its own ``digest`` posts are excluded by default.
SWEEPER_NODE_ID = "board-sweeper"
DIGEST_SELF_TYPES = frozenset({"digest"})
#: How many senders ``top_senders`` lists.
TOP_SENDERS = 5
#: A correlation id needs this many messages to count as a busy thread.
BUSY_THREAD_MIN = 3


def empty_digest(
    channel: str | None = None,
    since: str | None = None,
    max_messages: int = 200,
    include_sweeper_posts: bool = False,
) -> dict[str, Any]:
    """The digest of a window with no messages (same shape as :func:`summarize`)."""
    return summarize([], channel=channel, since=since)


def has_ack(msg: dict[str, Any]) -> bool:
    """True if ``msg`` has at least one acknowledgement in ``ack_by``."""
    ack = msg.get("ack_by")
    if isinstance(ack, list):
        return bool(ack)
    if isinstance(ack, str):
        return ack not in ("", "[]")
    return False


def is_sweeper_digest(msg: dict[str, Any]) -> bool:
    """True for a digest the sweeper itself posted."""
    return (
        msg.get("sender_node_id") == SWEEPER_NODE_ID
        and msg.get("message_type") in DIGEST_SELF_TYPES
    )


def without_sweeper_digests(msgs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the sweeper's own digest posts (otherwise digests count digests)."""
    return [m for m in msgs if not is_sweeper_digest(m)]


def _most_recent_per_type(msgs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """First message per type in poll order (newest first)."""
    out: dict[str, dict[str, Any]] = {}
    for msg in msgs:
        mt = msg.get("message_type") or "unknown"
        if mt not in out:
            out[mt] = {
                "message_id": msg.get("message_id"),
                "subject": msg.get("subject"),
                "from": msg.get("sender_node_id"),
                "at": msg.get("created_at"),
            }
    return out


def _open_blockers(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "message_id": msg.get("message_id"),
            "subject": msg.get("subject"),
            "task_id": msg.get("task_id"),
            "from": msg.get("sender_node_id"),
            "at": msg.get("created_at"),
        }
        for msg in msgs
        if msg.get("requires_ack") and not has_ack(msg)
    ]


def _top_senders(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(msg.get("sender_node_id") or "unknown" for msg in msgs)
    # Stable: equal counts keep first-seen order, like the original dict sort.
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_SENDERS]
    return [{"from": sender, "count": count} for sender, count in ranked]


def _busy_threads(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(c for msg in msgs if (c := msg.get("correlation_id")))
    return [
        {"correlation_id": cid, "count": count}
        for cid, count in counts.items()
        if count >= BUSY_THREAD_MIN
    ]


def summarize(
    msgs: list[dict[str, Any]],
    *,
    channel: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Compressed summary of ``msgs`` (newest first, as ``poll`` returns them).

    * Counts by message_type
    * Most-recent message per type (subject only)
    * Open blockers (requires_ack=1 AND not yet acked)
    * Active senders (top 5 by post count)
    * Threads with >= 3 messages (correlation_id + count)
    """
    return {
        "channel": channel or "(all)",
        "since": since,
        "scanned": len(msgs),
        "counts_by_type": dict(Counter(m.get("message_type") or "unknown" for m in msgs)),
        "most_recent_per_type": _most_recent_per_type(msgs),
        "open_blockers": _open_blockers(msgs),
        "top_senders": _top_senders(msgs),
        "busy_threads": _busy_threads(msgs),
    }


__all__ = [
    "BUSY_THREAD_MIN",
    "DIGEST_SELF_TYPES",
    "SWEEPER_NODE_ID",
    "TOP_SENDERS",
    "empty_digest",
    "has_ack",
    "is_sweeper_digest",
    "summarize",
    "without_sweeper_digests",
]
