"""The operator's evidence retention and deletion policy (ADR 0008, ADR 0012)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..contracts import RetentionMode
from ..errors import InvalidInputError

#: What deleting a record leaves behind.
OnDelete = Literal["tombstone", "purge"]
ON_DELETE_MODES: tuple[str, ...] = ("tombstone", "purge")
#: Default cap on the bytes one revision may retain (16 MiB).
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
#: Hard upper bound of ``KE_EVIDENCE_MAX_BYTES`` (256 MiB).
MAX_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class EvidencePolicy:
    """Retention defaults and the deletion mode.

    * ``default_retention`` -- retention of records created by ingestion when
      the request does not say (``retain`` or ``digest_only``);
    * ``on_delete`` -- ``tombstone`` keeps identity, digest, deletion time and
      reason (citations resolve to ``unavailable``); ``purge`` removes the
      record and every revision, span and alias (citations no longer resolve);
    * ``max_bytes`` -- larger revisions are recorded digest-only with a reason.
    """

    default_retention: RetentionMode = RetentionMode.RETAIN
    on_delete: OnDelete = "tombstone"
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        if self.on_delete not in ON_DELETE_MODES:
            raise InvalidInputError(
                f"unknown deletion mode {self.on_delete!r}",
                details={"allowed": list(ON_DELETE_MODES)},
            )
        if self.default_retention == RetentionMode.TOMBSTONE:
            raise InvalidInputError("the default retention cannot be 'tombstone'")
        if not 1 <= self.max_bytes <= MAX_MAX_BYTES:
            raise InvalidInputError(f"max_bytes must be between 1 and {MAX_MAX_BYTES}")


__all__ = ["DEFAULT_MAX_BYTES", "MAX_MAX_BYTES", "ON_DELETE_MODES", "EvidencePolicy", "OnDelete"]
