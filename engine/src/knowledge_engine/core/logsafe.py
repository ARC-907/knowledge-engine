"""Make request-derived values safe to interpolate into log records.

Scope names, key labels, header fields and ids come straight from HTTP or
MCP callers. Interpolating them raw lets a client forge additional log
lines with embedded CR/LF (log injection). :func:`log_safe` escapes the
line breaks instead of dropping them, so the record still shows exactly
what was sent.
"""

from __future__ import annotations

__all__ = ["log_safe"]


def log_safe(value: object) -> str:
    """Return ``value`` as a single-line string for log interpolation."""
    text = value if isinstance(value, str) else str(value)
    return text.replace("\r\n", "\\r\\n").replace("\n", "\\n").replace("\r", "\\r")
