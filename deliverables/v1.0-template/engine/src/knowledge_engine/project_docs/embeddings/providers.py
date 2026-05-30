"""Embedding provider abstraction for the project-docs subsystem.

This module defines the :class:`EmbeddingProvider` ABC, a deterministic
:class:`StubProvider` for offline tests, a :func:`get_provider` factory that
honors the embeddings configuration gates, and small helpers for packing,
unpacking, and comparing dense vectors.

No provider performs any network I/O at import time. Providers that require a
backend (Ollama, remote HTTP) only reach out when :meth:`EmbeddingProvider.embed`
is invoked, and they degrade gracefully when their dependency is unavailable.
"""

from __future__ import annotations

import hashlib
import math
import struct
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from knowledge_engine.project_docs.config import ProjectDocsConfig

# Little-endian single-precision floats, matching the on-disk vector blob.
_FLOAT_SIZE = struct.calcsize("<f")

#: Provider identifiers that mean "embeddings are intentionally off".
_DISABLED_PROVIDERS = frozenset({"", "none"})


class EmbeddingProvider(ABC):
    """Abstract base class for dense-embedding backends.

    Concrete providers turn a batch of texts into a batch of equal-length float
    vectors. Implementations must be side-effect free at construction time.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for this provider (used in metadata records)."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Dimensionality of the vectors returned by :meth:`embed`."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one ``dim``-length float vector per input text."""


class StubProvider(EmbeddingProvider):
    """Deterministic, offline embedding provider for tests and dry-runs.

    Vectors are derived purely from a hash of each input string, so identical
    text always yields the identical vector and no network access is required.
    """

    def __init__(self, dim: int = 8) -> None:
        if dim <= 0:
            raise ValueError("dim must be a positive integer")
        self._dim = int(dim)

    @property
    def name(self) -> str:
        return "stub"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return deterministic vectors derived from text hashes."""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        """Hash ``text`` into ``self._dim`` floats in the range [-1, 1)."""
        values: list[float] = []
        counter = 0
        # Expand the digest deterministically until we have enough components.
        while len(values) < self._dim:
            seed = f"{counter}:{text}".encode("utf-8")
            digest = hashlib.sha256(seed).digest()
            for i in range(0, len(digest), 2):
                if len(values) >= self._dim:
                    break
                raw = int.from_bytes(digest[i : i + 2], "big")
                # Map an unsigned 16-bit int into [-1, 1).
                values.append((raw / 32768.0) - 1.0)
            counter += 1
        return values[: self._dim]


def get_provider(cfg: "ProjectDocsConfig") -> EmbeddingProvider | None:
    """Resolve the configured embedding provider, honoring all gates.

    Returns ``None`` (it does not raise) when embeddings are disabled or when
    the configured provider name designates "no provider". A returned provider
    is safe to construct but may still degrade at :meth:`EmbeddingProvider.embed`
    time if its backend is unreachable.
    """
    emb = getattr(cfg, "embeddings", None)
    if emb is None:
        return None
    if not getattr(emb, "enabled", False):
        return None

    provider = str(getattr(emb, "provider", "") or "").strip().lower()
    if provider in _DISABLED_PROVIDERS:
        return None

    if provider == "stub":
        return StubProvider(dim=int(getattr(emb, "dim", 8) or 8))

    if provider in {"ollama", "local"}:
        from knowledge_engine.project_docs.embeddings.local import OllamaProvider

        return OllamaProvider(
            model=str(getattr(emb, "model", "") or ""),
            url=str(getattr(emb, "url", "") or ""),
        )

    if provider in {"remote", "http"}:
        from knowledge_engine.project_docs.embeddings.remote import RemoteProvider

        return RemoteProvider(cfg)

    # Unknown provider name: conservative no-op rather than an error.
    return None


def pack_vector(vector: list[float]) -> bytes:
    """Pack a list of floats into a little-endian 4-byte-float byte string."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack a little-endian 4-byte-float byte string into a list of floats."""
    if len(blob) % _FLOAT_SIZE != 0:
        raise ValueError("blob length is not a multiple of 4 bytes")
    count = len(blob) // _FLOAT_SIZE
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity of two equal-length vectors.

    Returns ``0.0`` for mismatched lengths or when either vector has zero
    magnitude, so callers never have to guard against division by zero.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
