"""Content hashing helpers for deduplication and pointer integrity.

Re-exports :mod:`knowledge_engine.core.util.hashing` (the single SHA-256 helper
module) under the names the project-context subsystem has always used.
"""

from __future__ import annotations

from ..core.util.hashing import content_hash, sha256_hex, short_hash

__all__ = ["content_hash", "sha256_hex", "short_hash"]
