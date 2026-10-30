"""Helpers shared by the built-in source adapters (no I/O at import)."""

from __future__ import annotations

from ..contracts import (
    Locator,
    OpenResult,
    RecordScope,
    SourceState,
    SourceStatus,
    TextLocator,
    UnsupportedLocator,
    VerificationState,
    derive_revision_id,
    sha256_digest,
)
from ..errors import InvalidInputError

#: Longest excerpt a hit carries (the contract allows 20 000 characters).
MAX_EXCERPT_CHARS = 2000
#: Largest record (bytes) whose revision id is computed and whose content is served.
MAX_REVISION_BYTES = 16 * 1024 * 1024


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters (``...`` when cut)."""
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def revision_of(source_id: str, record_id: str, data: bytes) -> tuple[str, str]:
    """``(revision_id, digest)`` of ``data``: the content-addressed id of ADR 0007."""
    digest = sha256_digest(data)
    return derive_revision_id(source_id, record_id, digest), digest


def check_locator(locator: Locator | None, size: int) -> Locator | None:
    """Accept a text locator inside ``size`` bytes (or none); reject other kinds."""
    if locator is None:
        return None
    if not isinstance(locator, TextLocator):
        raise InvalidInputError(
            f"this source serves text locators only, not {locator.kind!r}",
            details={"field": "locator"},
        )
    if locator.end_byte > size:
        raise InvalidInputError(
            "the locator ends after the revision's last byte",
            details={"field": "locator", "size": size},
        )
    return locator


def open_result(
    *,
    source_id: str,
    record_id: str,
    requested: str,
    scope: RecordScope,
    media_type: str,
    data: bytes | None,
    locator: Locator | None,
    provenance_id: str | None,
    missing_reason: str = "the record's bytes are no longer accessible",
) -> OpenResult:
    """Build the :class:`OpenResult` for ``requested`` against the current bytes ``data``.

    * bytes gone -> ``unavailable`` (no content);
    * current bytes have the requested revision id -> ``verified`` with content;
    * otherwise -> ``unsupported``: this source keeps no older revisions, so
      the requested one cannot be served and the latest is never substituted.
    """
    status = SourceStatus(source_id=source_id, state=SourceState.OK)
    common = {
        "source_id": source_id,
        "record_id": record_id,
        "revision_id": requested,
        "scope": scope,
        "media_type": media_type,
        "provenance_id": provenance_id,
        "source_status": status,
    }
    if data is None:
        return OpenResult(
            **common,
            verification=VerificationState.UNAVAILABLE,
            locator=UnsupportedLocator(reason=missing_reason),
        )
    current, digest = revision_of(source_id, record_id, data)
    if current != requested:
        return OpenResult(
            **common,
            verification=VerificationState.UNSUPPORTED,
            locator=UnsupportedLocator(
                reason="revision storage is not available for this source; the current "
                f"revision is {current}"
            ),
        )
    checked = check_locator(locator, len(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return OpenResult(
            **common,
            verification=VerificationState.UNSUPPORTED,
            digest=digest,
            locator=UnsupportedLocator(reason="the revision is not valid UTF-8 text"),
        )
    return OpenResult(
        **common,
        verification=VerificationState.VERIFIED,
        digest=digest,
        content=text,
        locator=checked,
    )


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MAX_REVISION_BYTES",
    "check_locator",
    "clip",
    "open_result",
    "revision_of",
]
