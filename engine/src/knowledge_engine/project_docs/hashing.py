"""Content hashing helpers for deduplication and pointer integrity."""

from __future__ import annotations

import hashlib


def content_hash(text: str) -> str:
    """Return the full SHA-256 hex digest of ``text`` (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, n: int = 8) -> str:
    """Return the first ``n`` hex chars of the SHA-256 digest of ``text``."""
    return content_hash(text)[:n]
