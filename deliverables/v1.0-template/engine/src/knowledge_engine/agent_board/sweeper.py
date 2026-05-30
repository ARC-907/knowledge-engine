"""Agent Board — sweeper.

Background loop that keeps the board healthy:

* Prunes expired (TTL-passed) messages
* Prunes overflow once max_messages_before_prune is exceeded
* Surfaces stale unacked blockers as `reminder` posts (with reply_to → original)
* Emits per-channel `digest` posts on the digest interval
* Logs each pass to `board_sweeps` so ops can verify cadence and find errors

Runs as a daemon thread inside the FastAPI process when
`board_config.sweeper_enabled=1`. Standalone mode (`service.py`) runs the
same loop on its own.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store
from ..pipeline import message_board as mb

_logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_lock = threading.Lock()


# ── Lifecycle ──────────────────────────────────────────────────


def start() -> bool:
    """Start the sweeper thread if not already running. Idempotent."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_run, name="agent-board-sweeper", daemon=True,
        )
        _thread.start()
        _logger.info("agent-board sweeper started")
        return True


def stop(timeout: float = 5.0) -> bool:
    """Signal the sweeper to stop. Returns True if it exited within timeout."""
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            return True
        _stop_event.set()
        _thread.join(timeout=timeout)
        exited = not _thread.is_alive()
        if exited:
            _thread = None
        return exited


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


# ── One pass (also exposed for the manual `/board/sweep` endpoint) ─


def sweep_once() -> dict[str, Any]:
    """Run a single sweep pass. Returns a result dict suitable for return to the
    caller of the manual `/board/sweep` route or `board_sweep_now` MCP tool.
    """
    cfg = store.load_config()
    started_at = _now_iso()
    pruned_expired = 0
    pruned_overflow = 0
    reminders_emitted = 0
    digests_emitted = 0
    error: str | None = None
    try:
        pruned_expired = store.prune_expired()
        pruned_overflow = store.prune_by_count(int(cfg["max_messages_before_prune"]))
        reminders_emitted = _emit_stale_blocker_reminders(int(cfg["stale_blocker_hours"]))
        digests_emitted = _maybe_emit_digests(
            int(cfg["digest_interval_minutes"]), cfg.get("channels") or [],
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        _logger.exception("agent-board sweep failed")

    finished_at = _now_iso()
    store.record_sweep(
        started_at=started_at, finished_at=finished_at,
        pruned_expired=pruned_expired, pruned_overflow=pruned_overflow,
        reminders_emitted=reminders_emitted, digests_emitted=digests_emitted,
        error=error,
    )
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "pruned_expired": pruned_expired,
        "pruned_overflow": pruned_overflow,
        "reminders_emitted": reminders_emitted,
        "digests_emitted": digests_emitted,
        "error": error,
    }


# ── Loop body ──────────────────────────────────────────────────


def _run() -> None:
    """The thread target."""
    while not _stop_event.is_set():
        try:
            cfg = store.load_config()
            interval = max(5, int(cfg.get("sweep_interval_s") or 60))
            if cfg.get("sweeper_enabled"):
                sweep_once()
        except Exception:  # noqa: BLE001
            _logger.exception("sweeper loop error; continuing")
            interval = 60
        # Sleep in small slices so stop() responds quickly.
        slept = 0.0
        while slept < interval and not _stop_event.is_set():
            time.sleep(0.5)
            slept += 0.5


def _emit_stale_blocker_reminders(threshold_hours: int) -> int:
    """For each stale unacked blocker, post one `reminder` message.

    Idempotency: the reminder's `reply_to` points at the original blocker's
    message_id, and we look back over recent reminders to avoid re-posting
    one we just posted. The first reminder is always emitted; subsequent
    reminders only after another `threshold_hours` window has passed.
    """
    blockers = store.get_unacked_blockers(threshold_hours=threshold_hours)
    if not blockers:
        return 0
    emitted = 0
    recent_reminders = mb.poll_messages(message_type="reminder", limit=500)
    reminded_recently: dict[str, str] = {}
    for r in recent_reminders:
        reply = r.get("reply_to")
        ts = r.get("created_at")
        if reply and ts and reply not in reminded_recently:
            reminded_recently[reply] = ts
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=threshold_hours)
    ).isoformat()
    for b in blockers:
        last_at = reminded_recently.get(b["message_id"])
        if last_at and last_at > cutoff:
            continue
        try:
            mb.post_message(
                message_type="reminder",
                sender_node_id="board-sweeper",
                channel=b.get("channel") or "ops",
                task_id=b.get("task_id"),
                product_id=b.get("product_id"),
                subject=f"Stale blocker: {b.get('subject') or b['message_id'][:8]}",
                body=json.dumps({
                    "blocker_id": b["message_id"],
                    "blocker_subject": b.get("subject"),
                    "blocker_age_hours": _age_hours(b.get("created_at")),
                    "original_sender": b.get("sender_node_id"),
                    "task_id": b.get("task_id"),
                }),
                visibility_scope="all",
                reply_to=b["message_id"],
                correlation_id=b.get("correlation_id") or b["message_id"],
                ttl_hours=threshold_hours * 2,
            )
            emitted += 1
        except Exception:  # noqa: BLE001
            _logger.exception("failed to emit reminder for %s", b["message_id"])
    return emitted


def _maybe_emit_digests(interval_minutes: int, channels: list[str]) -> int:
    """If at least `interval_minutes` have passed since the last digest per
    channel, emit a new one. Returns the count emitted in this pass.
    """
    if interval_minutes <= 0 or not channels:
        return 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=interval_minutes)
    ).isoformat()
    emitted = 0
    recent_digests = mb.poll_messages(message_type="digest", limit=500)
    last_digest_per_channel: dict[str, str] = {}
    for d in recent_digests:
        ch = d.get("channel") or "ops"
        ts = d.get("created_at")
        if not ts:
            continue
        if ch not in last_digest_per_channel or last_digest_per_channel[ch] < ts:
            last_digest_per_channel[ch] = ts
    for channel in channels:
        last = last_digest_per_channel.get(channel)
        if last and last > cutoff:
            continue
        try:
            summary = store.digest(channel=channel, since=cutoff, max_messages=200)
            # Skip empty windows — no point spamming a quiet channel.
            if summary.get("scanned", 0) == 0:
                continue
            mb.post_message(
                message_type="digest",
                sender_node_id="board-sweeper",
                channel=channel,
                subject=f"Digest — {channel} ({interval_minutes}m window)",
                body=json.dumps(summary, indent=2),
                visibility_scope="all",
                ttl_hours=max(interval_minutes // 60 + 1, 24),
            )
            emitted += 1
        except Exception:  # noqa: BLE001
            _logger.exception("failed to emit digest for channel %s", channel)
    return emitted


# ── Helpers ────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - then).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


__all__ = ["start", "stop", "is_running", "sweep_once"]
