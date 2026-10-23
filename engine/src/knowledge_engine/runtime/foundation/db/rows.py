"""Row helpers: ``sqlite3.Row`` -> dict with JSON columns decoded."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable
from typing import Any

# JSON-serialized fields -- auto-deserialized by dict_from_row
JSON_FIELDS: frozenset[str] = frozenset(
    {
        "depends_on",
        "assigned_capability",
        "source_policy",
        "output_artifacts",
        "audit_trail",
        "capabilities",
        "current_tasks",
        "config_json",
        "ack_by",
        "available_models",
        "tags",
        "shared_with",
        "input_schema",
        "output_schema",
        "input_json",
        "meta_json",
        "allowed_methods",
        "allowed_headers",
    }
)

#: A decoded pipeline row (JSON columns already parsed).
Row = dict[str, Any]


class RowNotFoundError(LookupError):
    """A row that must exist (e.g. one just written) could not be read back."""


def dict_from_row(row: sqlite3.Row | None) -> Row | None:
    """Convert a sqlite3.Row to a dict, deserializing JSON fields."""
    if row is None:
        return None
    d = dict(row)
    for field in JSON_FIELDS:
        if field in d and isinstance(d[field], str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                d[field] = json.loads(d[field])
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row | None]) -> list[Row]:
    """Convert multiple rows to dicts."""
    return [d for r in rows if r is not None if (d := dict_from_row(r)) is not None]


def require_row(row: Row | None, what: str, key: str) -> Row:
    """Return ``row``, or raise :class:`RowNotFoundError` naming ``what`` / ``key``.

    For reads that follow a write in the same call (``create_x`` returning
    ``get_x(id)``): the row can only be missing if another writer deleted it
    in between, which is reported instead of returning ``None`` to a caller
    typed for a row.
    """
    if row is None:
        raise RowNotFoundError(f"{what} {key!r} not found after write")
    return row


__all__ = [
    "JSON_FIELDS",
    "Row",
    "RowNotFoundError",
    "dict_from_row",
    "require_row",
    "rows_to_dicts",
]
