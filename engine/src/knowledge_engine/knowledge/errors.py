"""Typed errors of the scoped-knowledge contracts, with stable codes.

Every failure the knowledge layer (metadata store, scope resolution, and the
later broker, evidence and module slices) reports is a :class:`KnowledgeError`
subclass. Each class carries:

* :attr:`KnowledgeError.code` -- a stable machine-readable code (never
  renamed; new codes are additive);
* :attr:`KnowledgeError.http_status` -- the status the HTTP binding answers with;
* :attr:`KnowledgeError.retryable` -- whether repeating the same call can succeed.

:meth:`KnowledgeError.to_dict` is the one wire shape for every binding:
``{"code", "message", "details", "retryable"}``. HTTP wraps it as
``{"detail": message, "type": code, "error": {...}}`` (the project's error
body plus the structured error), MCP returns ``{"error": {...}}`` as an
``isError`` tool result, and the CLI prints the message on stderr and exits
with :data:`CLI_EXIT_CODES`.

``details`` must be JSON-safe and must never contain credentials, absolute
paths of private stores or full research content.
"""

from __future__ import annotations

from typing import Any, ClassVar

#: Every stable code, in documentation order.
ERROR_CODES: tuple[str, ...] = (
    "invalid_scope",
    "unknown_scope",
    "invalid_input",
    "denied",
    "unavailable",
    "incompatible",
    "timeout",
    "cancelled",
    "output_invalid",
    "internal_error",
)


class KnowledgeError(Exception):
    """Base class of every knowledge-layer failure (code ``internal_error``)."""

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """``{"code", "message", "details", "retryable"}`` (JSON-safe)."""
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class InvalidScopeError(KnowledgeError):
    """The scope is malformed or contradictory (e.g. a collection of another project)."""

    code = "invalid_scope"
    http_status = 400


class UnknownScopeError(KnowledgeError):
    """A workspace, project, source, collection, profile or record id does not exist.

    Unknown ids are validation errors, never an empty or a global query.
    """

    code = "unknown_scope"
    http_status = 422


class InvalidInputError(KnowledgeError):
    """A request field fails validation (bad id syntax, unknown research type ...)."""

    code = "invalid_input"
    http_status = 400


class DeniedError(KnowledgeError):
    """Host policy refuses the operation."""

    code = "denied"
    http_status = 403


class UnavailableError(KnowledgeError):
    """A store or source cannot answer now (every selected source failed, store closed)."""

    code = "unavailable"
    http_status = 503
    retryable = True


class IncompatibleError(KnowledgeError):
    """Versions do not match (newer schema than this binary, unsupported contract)."""

    code = "incompatible"
    http_status = 409


class KnowledgeTimeoutError(KnowledgeError):
    """A deadline expired before the operation completed."""

    code = "timeout"
    http_status = 504
    retryable = True


class CancelledError(KnowledgeError):
    """The caller or the host cancelled the operation."""

    code = "cancelled"
    http_status = 409


class OutputInvalidError(KnowledgeError):
    """An adapter or module produced output that fails its declared schema."""

    code = "output_invalid"
    http_status = 502


class InternalError(KnowledgeError):
    """An unexpected failure; details are logged, not returned."""

    code = "internal_error"
    http_status = 500


#: ``code -> class`` for every stable code.
ERRORS_BY_CODE: dict[str, type[KnowledgeError]] = {
    cls.code: cls
    for cls in (
        InvalidScopeError,
        UnknownScopeError,
        InvalidInputError,
        DeniedError,
        UnavailableError,
        IncompatibleError,
        KnowledgeTimeoutError,
        CancelledError,
        OutputInvalidError,
        InternalError,
    )
}

#: Exit status of a CLI command that fails with the given code.
CLI_EXIT_CODES: dict[str, int] = {
    "invalid_scope": 2,
    "unknown_scope": 2,
    "invalid_input": 2,
    "denied": 3,
    "unavailable": 4,
    "incompatible": 5,
    "timeout": 4,
    "cancelled": 6,
    "output_invalid": 1,
    "internal_error": 1,
}


def from_dict(payload: dict[str, Any]) -> KnowledgeError:
    """Rebuild an error from :meth:`KnowledgeError.to_dict` output (unknown code -> internal)."""
    cls = ERRORS_BY_CODE.get(str(payload.get("code")), InternalError)
    details = payload.get("details")
    return cls(
        str(payload.get("message", "")), details=details if isinstance(details, dict) else None
    )


__all__ = [
    "CLI_EXIT_CODES",
    "ERRORS_BY_CODE",
    "ERROR_CODES",
    "CancelledError",
    "DeniedError",
    "IncompatibleError",
    "InternalError",
    "InvalidInputError",
    "InvalidScopeError",
    "KnowledgeError",
    "KnowledgeTimeoutError",
    "OutputInvalidError",
    "UnavailableError",
    "UnknownScopeError",
    "from_dict",
]
