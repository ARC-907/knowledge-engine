"""Dense-vector helpers shared by every embedding store in the engine.

One on-disk format and one similarity function, used by the corpus embedding
index (``retrieval.embeddings``) and the project-context embedding store
(``project_context.embeddings``):

* :func:`pack_vector` / :func:`unpack_vector` -- a one-byte format version
  (:data:`VECTOR_FORMAT_VERSION`) followed by little-endian IEEE-754 float32
  components. :func:`unpack_vector` also reads the headerless blobs written
  before the version byte existed (``4 * n`` bytes, little-endian float32), so
  stores built by older releases stay readable.
* :func:`cosine` -- cosine similarity that **raises** :class:`VectorDimensionError`
  when the two vectors differ in length. Vectors of different lengths come
  from different embedding models and cannot be compared; silently truncating
  one of them (as older code did) produces meaningless scores.
* :class:`CosineScorer` -- the same computation with the query norm computed
  once, for scanning many stored vectors.

Pure functions: no I/O, no logging, no state.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

#: Version byte written in front of every packed vector.
VECTOR_FORMAT_VERSION = 1

_HEADER = bytes([VECTOR_FORMAT_VERSION])
_FLOAT_SIZE = struct.calcsize("<f")


class VectorDimensionError(ValueError):
    """Two vectors that must have the same dimension do not.

    ``left`` and ``right`` are the two lengths.
    """

    def __init__(self, left: int, right: int) -> None:
        super().__init__(
            f"vector dimensions differ ({left} vs {right}): vectors produced by different "
            "embedding models cannot be compared; re-embed with one model"
        )
        self.left = left
        self.right = right


def pack_vector(vector: Sequence[float]) -> bytes:
    """Pack ``vector`` as ``<version byte><little-endian float32 * n>``.

    Raises ``ValueError`` when a component is not a finite number or does not
    fit in a float32.
    """
    values: list[float] = []
    for value in vector:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector components must be finite numbers")
        values.append(number)
    try:
        body = struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as exc:
        raise ValueError(f"vector component out of float32 range: {exc}") from exc
    return _HEADER + body


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack a blob written by :func:`pack_vector` (or a legacy headerless one).

    A blob of ``4 * n`` bytes is the legacy format (no version byte); one of
    ``4 * n + 1`` bytes must start with :data:`VECTOR_FORMAT_VERSION`. Any
    other length or version raises ``ValueError``.
    """
    data = bytes(blob)
    size = len(data)
    if size % _FLOAT_SIZE == 0:
        body = data
    elif size % _FLOAT_SIZE == 1:
        if data[0] != VECTOR_FORMAT_VERSION:
            raise ValueError(f"unknown vector format version {data[0]}")
        body = data[1:]
    else:
        raise ValueError(f"blob length {size} is not a packed float32 vector")
    count = len(body) // _FLOAT_SIZE
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}f", body))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors of equal length.

    Raises :class:`VectorDimensionError` when the lengths differ. Returns
    ``0.0`` for two empty vectors or when either vector has zero magnitude.
    """
    if len(a) != len(b):
        raise VectorDimensionError(len(a), len(b))
    if not a:
        return 0.0
    norm_a = math.sqrt(math.sumprod(a, a))
    norm_b = math.sqrt(math.sumprod(b, b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return math.sumprod(a, b) / (norm_a * norm_b)


class CosineScorer:
    """Cosine similarity against one fixed query vector (query norm computed once)."""

    __slots__ = ("_norm", "_query", "dim")

    def __init__(self, query: Sequence[float]) -> None:
        self._query = [float(value) for value in query]
        self.dim = len(self._query)
        self._norm = math.sqrt(math.sumprod(self._query, self._query)) if self._query else 0.0

    def __call__(self, vector: Sequence[float]) -> float:
        """Similarity of ``vector`` to the query; raises :class:`VectorDimensionError`."""
        if len(vector) != self.dim:
            raise VectorDimensionError(self.dim, len(vector))
        if self._norm <= 0.0:
            return 0.0
        norm = math.sqrt(math.sumprod(vector, vector))
        if norm <= 0.0:
            return 0.0
        return math.sumprod(self._query, vector) / (self._norm * norm)


__all__ = [
    "VECTOR_FORMAT_VERSION",
    "CosineScorer",
    "VectorDimensionError",
    "cosine",
    "pack_vector",
    "unpack_vector",
]
