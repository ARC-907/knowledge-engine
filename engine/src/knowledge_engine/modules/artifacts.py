"""Normalized module artifacts and their content-addressed byte store.

A module returns artifact *descriptors* (media type, text or base64 bytes,
canonical URI, timestamps, rights ...). :func:`normalize_artifacts` checks
them against the module's limits and turns them into
:class:`NormalizedArtifact` records: SHA-256 digest, byte length, the run and
module that produced them, the destination scope and parent provenance. The
id is derived from module, record identity and digest, so the same bytes of
the same record produce the same artifact again (idempotent restarts).

:class:`ArtifactStore` keeps the bytes under ``<dir>/<hh>/<hex>`` (written
atomically). Records live in ``modules.db`` (:mod:`.catalog`).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from ..knowledge.contracts import (
    DIGEST_PATTERN,
    RECORD_ID_PATTERN,
    RecordScope,
    RightsInfo,
    canonical_json,
    validation_messages,
)
from ..knowledge.errors import OutputInvalidError, UnavailableError

ARTIFACT_ID_PATTERN = r"^art_[0-9a-f]{32}$"
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}(;.*)?$", re.I
)
MAX_METADATA_BYTES = 16_384


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactDescriptor(_Model):
    """What a module returns for one artifact (the raw form, before normalization)."""

    media_type: str = Field(min_length=3, max_length=255)
    text: str | None = None
    base64: str | None = None
    record_key: str | None = Field(default=None, pattern=RECORD_ID_PATTERN)
    canonical_uri: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=1000)
    published_at: str | None = Field(default=None, max_length=40)
    retrieved_at: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, max_length=35)
    rights: RightsInfo | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_artifact_ids: tuple[str, ...] = ()


class NormalizedArtifact(_Model):
    """A validated artifact: identity, digest, provenance and destination scope."""

    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    module_id: str
    module_version: str
    run_id: str
    scope: RecordScope
    media_type: str
    digest: str = Field(pattern=DIGEST_PATTERN)
    byte_length: int = Field(ge=0)
    record_key: str | None = None
    canonical_uri: str | None = None
    title: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    language: str | None = None
    rights: RightsInfo = Field(default_factory=RightsInfo)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_artifact_ids: tuple[str, ...] = ()
    created_at: AwareDatetime


@dataclass(frozen=True)
class ArtifactPayload:
    """A normalized artifact together with its bytes (what ingestion receives)."""

    artifact: NormalizedArtifact
    content: bytes


def derive_artifact_id(module_id: str, identity: str, digest: str) -> str:
    joined = "\x00".join(("artifact", module_id, identity, digest)).encode("utf-8")
    return "art_" + hashlib.sha256(joined).hexdigest()[:32]


def _content(desc: ArtifactDescriptor, index: int) -> bytes:
    if (desc.text is None) == (desc.base64 is None):
        raise OutputInvalidError(f"artifact {index}: exactly one of 'text' or 'base64' is required")
    if desc.text is not None:
        return desc.text.encode("utf-8")
    try:
        return base64.b64decode(desc.base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OutputInvalidError(f"artifact {index}: 'base64' is not valid base64") from exc


def normalize_artifacts(
    raw: Any,
    *,
    module_id: str,
    module_version: str,
    run_id: str,
    scope: RecordScope,
    max_artifacts: int,
    max_artifact_bytes: int,
    known_parents: Mapping[str, NormalizedArtifact],
    now: Any,
) -> list[ArtifactPayload]:
    """Validate ``raw`` (the module's ``artifacts`` list) and normalize it.

    Raises:
        OutputInvalidError: not a list, too many artifacts, over the byte
            budget, an invalid descriptor or a parent that was not an input.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OutputInvalidError("'artifacts' must be a list")
    if len(raw) > max_artifacts:
        raise OutputInvalidError(
            f"the module returned {len(raw)} artifacts; the limit is {max_artifacts}"
        )
    out: list[ArtifactPayload] = []
    total = 0
    for index, item in enumerate(raw):
        try:
            desc = ArtifactDescriptor.model_validate(item)
        except ValidationError as exc:
            raise OutputInvalidError(
                f"artifact {index} is invalid", details={"errors": validation_messages(exc)}
            ) from exc
        if not _MEDIA_TYPE_RE.match(desc.media_type):
            raise OutputInvalidError(f"artifact {index}: invalid media_type")
        if len(canonical_json(desc.metadata).encode("utf-8")) > MAX_METADATA_BYTES:
            raise OutputInvalidError(
                f"artifact {index}: metadata exceeds {MAX_METADATA_BYTES} bytes"
            )
        unknown = [p for p in desc.parent_artifact_ids if p not in known_parents]
        if unknown:
            raise OutputInvalidError(
                f"artifact {index}: parent artifacts must be inputs of this call",
                details={"unknown_parents": unknown},
            )
        content = _content(desc, index)
        total += len(content)
        if total > max_artifact_bytes:
            raise OutputInvalidError(
                f"artifacts exceed the module's byte budget of {max_artifact_bytes}"
            )
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        identity = desc.record_key or desc.canonical_uri or ""
        artifact = NormalizedArtifact(
            artifact_id=derive_artifact_id(module_id, identity, digest),
            module_id=module_id,
            module_version=module_version,
            run_id=run_id,
            scope=scope,
            media_type=desc.media_type,
            digest=digest,
            byte_length=len(content),
            record_key=desc.record_key,
            canonical_uri=desc.canonical_uri,
            title=desc.title,
            published_at=desc.published_at,
            retrieved_at=desc.retrieved_at,
            language=desc.language,
            rights=desc.rights or RightsInfo(),
            metadata=desc.metadata,
            parent_artifact_ids=desc.parent_artifact_ids,
            created_at=now,
        )
        out.append(ArtifactPayload(artifact, content))
    return out


class ArtifactStore:
    """Content-addressed artifact bytes under one directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, digest: str) -> Path:
        hexdigest = digest.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
            raise ValueError("invalid digest")
        return self.root / hexdigest[:2] / hexdigest

    def put(self, digest: str, content: bytes) -> Path:
        """Store ``content`` (verified against ``digest``) and return its path."""
        if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("content does not match its digest")
        target = self._path(digest)
        if target.is_file():
            return target
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                Path(tmp).replace(target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    Path(tmp).unlink()
        except OSError as exc:
            raise UnavailableError("the module artifact store cannot be written") from exc
        return target

    def path_for(self, digest: str) -> Path:
        """Path of stored bytes. Raises :class:`UnavailableError` when they are gone."""
        target = self._path(digest)
        if not target.is_file():
            raise UnavailableError("the artifact bytes are no longer available")
        return target

    def read(self, digest: str) -> bytes:
        return self.path_for(digest).read_bytes()


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "ArtifactDescriptor",
    "ArtifactPayload",
    "ArtifactStore",
    "NormalizedArtifact",
    "derive_artifact_id",
    "normalize_artifacts",
]
