"""SHA-256 helpers: the only place the engine calls :func:`hashlib.sha256`.

Text is hashed as UTF-8. A :class:`os.PathLike` argument hashes the *contents*
of that file (streamed in 64 KiB blocks); pass ``str(path)`` to hash the path
text itself.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_BLOCK = 64 * 1024

Hashable = str | bytes | bytearray | memoryview


def _hasher(data: Hashable | os.PathLike[str]) -> hashlib._Hash:
    h = hashlib.sha256()
    if isinstance(data, os.PathLike):
        with Path(data).open("rb") as handle:
            for block in iter(lambda: handle.read(_BLOCK), b""):
                h.update(block)
    elif isinstance(data, str):
        h.update(data.encode("utf-8"))
    elif isinstance(data, bytes | bytearray | memoryview):
        h.update(data)
    else:
        raise TypeError(f"cannot hash {type(data).__name__}; expected str, bytes or a path")
    return h


def sha256_hex(data: Hashable | os.PathLike[str], *, length: int | None = None) -> str:
    """Hex SHA-256 of ``data`` (text, bytes, or a file's contents for a path).

    ``length`` truncates the digest to its first ``length`` hex characters.
    Raises ``OSError`` when a path cannot be read.
    """
    digest = _hasher(data).hexdigest()
    if length is not None:
        if length < 1:
            raise ValueError("length must be a positive integer")
        return digest[:length]
    return digest


def sha256_digest(data: Hashable | os.PathLike[str]) -> bytes:
    """Raw 32-byte SHA-256 digest of ``data`` (same inputs as :func:`sha256_hex`)."""
    return _hasher(data).digest()


def content_hash(text: str) -> str:
    """Full SHA-256 hex digest of ``text`` (UTF-8)."""
    return sha256_hex(text)


def short_hash(text: str, n: int = 8) -> str:
    """First ``n`` hex characters of the SHA-256 digest of ``text``."""
    return content_hash(text)[:n]


__all__ = ["content_hash", "sha256_digest", "sha256_hex", "short_hash"]
