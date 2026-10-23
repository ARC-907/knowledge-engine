"""Board HTTP: shared request-body plumbing (lenient documented Pydantic models).

The models document the board's request bodies in OpenAPI. Fields are typed
``Any`` at runtime with the documented JSON schema attached, because the
board's own validators (``schemas.validate``, ``store.update_config``, the
key vault) have always coerced loosely-typed input and answer 400 with their
own messages; the models must not turn those answers into 422s. Unknown
fields are kept (``extra="allow"``) and passed through unchanged.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, WithJsonSchema

from .. import schemas


def _documented(schema: dict[str, Any]) -> Any:
    return WithJsonSchema(schema)


def _string(description: str, **extra: Any) -> Any:
    return _documented({"type": "string", "description": description, **extra})


SCOPE_DOC = _string(
    "Optional per-scope database (project/branch/agent/loop). Overrides the `scope` query "
    "parameter when both are given."
)


class _BoardBody(BaseModel):
    """Lenient base: documents fields, keeps unknown ones, dumps only what was sent."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def payload(self) -> dict[str, Any]:
        """The body as the plain dict the store layer expects (only fields sent)."""
        return self.model_dump(exclude_unset=True, by_alias=True)


def _required(*names: str) -> Any:
    def mark(schema: dict[str, Any]) -> None:
        schema["required"] = list(names)

    return mark


def _message_field(name: str) -> Any:
    spec = next(f for f in schemas.MESSAGE_FIELDS if f.name == name)
    return _documented(spec.json_schema())


def _int(description: str) -> Any:
    return _documented({"type": "integer", "description": description})


def _pop_scope(payload: dict[str, Any]) -> str | None:
    """Pull an optional ``scope`` out of a POST body so it routes the write
    to a per-scope DB without leaking into the message payload itself.
    """
    val = payload.pop("scope", None)
    if val is None:
        return None
    s = str(val).strip()
    return s or None
