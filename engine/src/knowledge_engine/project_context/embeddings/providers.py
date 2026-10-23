"""Embedding provider abstraction: the engine's one provider interface.

:class:`EmbeddingProvider` is the interface every embedding backend implements,
for both the project-context store (:mod:`.index`) and the corpus embedding
index (:mod:`knowledge_engine.retrieval.embeddings`). This module defines it
together with two deterministic, offline providers:

* :class:`StubProvider` -- hash-of-the-whole-text vectors (project-context
  tests and dry runs; provider name ``stub``);
* :class:`FakeProvider` -- feature-hashed bag-of-words vectors, so texts that
  share words are similar (corpus-index tests and ``--provider fake`` CLI dry
  runs; provider name ``fake``).

The network-backed providers live next to it: :mod:`.local`
(``LocalOllamaProvider``, batched through :class:`knowledge_engine.core.ollama.OllamaClient`)
and :mod:`.remote` (``RemoteProvider``, gated behind an explicit opt-in).
:func:`get_provider` resolves the project-context configuration to a provider.

Packing and similarity are shared with every other vector store through
:mod:`knowledge_engine.core.util.vectors` and re-exported here:
:func:`pack_vector` / :func:`unpack_vector` (version byte + little-endian
float32) and :func:`cosine`, which raises :class:`VectorDimensionError` on a
dimension mismatch instead of returning a meaningless score.

No provider performs any network I/O at import time. Providers that require a
backend (Ollama, remote HTTP) only reach out when :meth:`EmbeddingProvider.embed`
is invoked.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ...core.util.hashing import sha256_digest
from ...core.util.vectors import (
    VECTOR_FORMAT_VERSION,
    VectorDimensionError,
    cosine,
    pack_vector,
    unpack_vector,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from knowledge_engine.project_context.config import ProjectContextConfig

#: Provider identifiers that mean "embeddings are intentionally off".
_DISABLED_PROVIDERS = frozenset({"", "none"})

#: Word tokens hashed by :class:`FakeProvider`.
_TOKEN_RE = re.compile(r"\w+")


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

    @property
    def model_id(self) -> str:
        """Identifier of the model that produced the vectors.

        Recorded by the corpus embedding index, which refuses to mix vectors
        of different models. Defaults to :attr:`name`; providers that serve a
        configurable model return that model name.
        """
        return self.name


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
            seed = f"{counter}:{text}".encode()
            digest = sha256_digest(seed)
            for i in range(0, len(digest), 2):
                if len(values) >= self._dim:
                    break
                raw = int.from_bytes(digest[i : i + 2], "big")
                # Map an unsigned 16-bit int into [-1, 1).
                values.append((raw / 32768.0) - 1.0)
            counter += 1
        return values[: self._dim]


class FakeProvider(EmbeddingProvider):
    """Deterministic, offline provider whose vectors reflect shared words.

    Each lower-cased word token is hashed to one of ``dim`` buckets with a
    hash-derived sign (feature hashing); the sum is L2-normalised. Texts that
    share words therefore score higher than unrelated ones, which makes the
    provider useful for tests and ``--provider fake`` dry runs of the corpus
    embedding index. A text without word characters gets a hash-of-text
    vector instead, so no vector is ever all zeros.
    """

    DEFAULT_DIM = 64

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be a positive integer")
        self._dim = int(dim)
        self._fallback = StubProvider(dim=self._dim)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model_id(self) -> str:
        return f"fake-hash-{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-length feature-hashed vector per text."""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        values = [0.0] * self._dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = sha256_digest(token.encode("utf-8"))
            bucket = int.from_bytes(digest[:4], "big") % self._dim
            values[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(math.sumprod(values, values))
        if norm == 0.0:
            values = self._fallback.embed([text])[0]
            norm = math.sqrt(math.sumprod(values, values)) or 1.0
        return [value / norm for value in values]


def get_provider(cfg: ProjectContextConfig) -> EmbeddingProvider | None:
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

    if provider == "fake":
        return FakeProvider(dim=int(getattr(emb, "dim", 0) or FakeProvider.DEFAULT_DIM))

    if provider in {"ollama", "local"}:
        # Lazy: local.py subclasses EmbeddingProvider from this module (import cycle).
        from knowledge_engine.project_context.embeddings.local import (  # noqa: PLC0415
            LocalOllamaProvider,
        )

        return LocalOllamaProvider(
            model=str(getattr(emb, "model", "") or ""),
            url=str(getattr(emb, "url", "") or ""),
        )

    if provider in {"remote", "http"}:
        # Lazy: remote.py subclasses EmbeddingProvider from this module (import cycle).
        from knowledge_engine.project_context.embeddings.remote import (  # noqa: PLC0415
            RemoteProvider,
        )

        return RemoteProvider(cfg)

    # Unknown provider name: conservative no-op rather than an error.
    return None


__all__ = [
    "VECTOR_FORMAT_VERSION",
    "EmbeddingProvider",
    "FakeProvider",
    "StubProvider",
    "VectorDimensionError",
    "cosine",
    "get_provider",
    "pack_vector",
    "unpack_vector",
]
