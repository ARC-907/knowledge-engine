"""UTC timestamps in the one string format the engine persists.

Every SQLite row, JSON document and audit record written by the engine uses
``datetime.now(UTC).isoformat()`` (for example ``2026-01-02T03:04:05.678901+00:00``);
the format is part of the on-disk contract because rows are compared as text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """The current time as an aware ``datetime`` in UTC."""
    return datetime.now(UTC)


def utc_now_iso(offset: timedelta | None = None) -> str:
    """The current UTC time (shifted by ``offset`` when given) as ISO-8601 text."""
    now = datetime.now(UTC)
    if offset is not None:
        now += offset
    return now.isoformat()


__all__ = ["utc_now", "utc_now_iso"]
