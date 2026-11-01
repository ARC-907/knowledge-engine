"""Typed row shapes for the pipeline stores (tasks, workers, messages).

Each ``TypedDict`` lists exactly the columns of its table (``SELECT *``), with
the JSON columns named in :data:`~knowledge_engine.runtime.foundation.db.rows.JSON_FIELDS`
already decoded by ``dict_from_row``. At runtime the rows stay plain ``dict``
objects, so JSON serialization, HTTP/MCP payloads and existing callers see
exactly the same values; the types only let mypy check field access.

Nullability follows the table definitions in the ``foundation.db`` schema
files: a column without ``NOT NULL`` is ``X | None``. The
decoded JSON columns are typed as the shape every writer stores; a value that
fails to decode stays a string, which callers that care guard against at
runtime.

This module imports nothing from the stores, so the stores and their callers
(``agent_board``, ``server``, ``mcp``) can all import it without a cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any, TypedDict, cast

from ..foundation import db


class AuditEntry(TypedDict):
    """One entry of a task's ``audit_trail``."""

    timestamp: str
    event: str
    worker: str | None
    detail: str | None


class TaskRow(TypedDict):
    """A row of the ``tasks`` table (``runtime.pipeline.queue``)."""

    task_id: str
    task_type: str
    library: str
    target_path: str
    chapter_key: str
    priority: int | None
    depends_on: list[str]
    allowed_layer: str | None
    assigned_capability: list[str]
    source_policy: dict[str, bool]
    input_json: Any
    meta_json: Any
    status: str
    assigned_worker: str | None
    lease_expires_at: str | None
    claimed_at: str | None
    started_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str
    retry_count: int | None
    failure_reason: str | None
    output_artifacts: list[Any]
    notes: str | None
    audit_trail: list[AuditEntry]


class WorkerRow(TypedDict):
    """A row of the ``workers`` table (``runtime.pipeline.worker_registry``)."""

    node_id: str
    hostname: str | None
    tailscale_ip: str | None
    display_name: str | None
    model_class: str | None
    role: str | None
    capabilities: list[str]
    ollama_endpoint: str | None
    ollama_model: str | None
    max_concurrent: int | None
    status: str | None
    current_tasks: list[str]
    last_heartbeat: str | None
    registered_at: str | None
    config_json: Any
    safeguard_locked: int | None
    safeguard_reason: str | None
    # Not in JSON_FIELDS: stays the raw JSON text.
    resources_json: str | None


class SafeguardStatus(TypedDict):
    """``worker_registry.get_safeguard_status`` / ``list_safeguard_statuses``."""

    node_id: str
    safeguard_locked: bool
    safeguard_reason: str | None
    status: str | None


class MessageRow(TypedDict):
    """A row of the ``messages`` table (``runtime.pipeline.message_board``)."""

    message_id: str
    channel: str
    thread_id: str | None
    task_id: str | None
    product_id: str | None
    sender_agent_id: str | None
    sender_node_id: str | None
    sender_role: str | None
    model_id: str | None
    message_type: str
    subject: str | None
    body: str | None
    created_at: str
    expires_at: str | None
    visibility_scope: str | None
    requires_ack: int | None
    ack_by: list[str]
    reply_to: str | None
    correlation_id: str | None


# ── Decoding ───────────────────────────────────────────────
#
# ``cast`` is the single trust boundary: ``SELECT *`` on the matching table
# yields exactly the declared keys (pinned by tests/test_pipeline_rows.py).


def task_from_row(row: sqlite3.Row | None) -> TaskRow | None:
    return cast("TaskRow | None", db.dict_from_row(row))


def tasks_from_rows(rows: Iterable[sqlite3.Row | None]) -> list[TaskRow]:
    return cast("list[TaskRow]", db.rows_to_dicts(rows))


def worker_from_row(row: sqlite3.Row | None) -> WorkerRow | None:
    return cast("WorkerRow | None", db.dict_from_row(row))


def workers_from_rows(rows: Iterable[sqlite3.Row | None]) -> list[WorkerRow]:
    return cast("list[WorkerRow]", db.rows_to_dicts(rows))


def message_from_row(row: sqlite3.Row | None) -> MessageRow | None:
    return cast("MessageRow | None", db.dict_from_row(row))


def messages_from_rows(rows: Iterable[sqlite3.Row | None]) -> list[MessageRow]:
    return cast("list[MessageRow]", db.rows_to_dicts(rows))


# ── Payloads ───────────────────────────────────────────────

#: Any of the typed pipeline rows.
type PipelineRow = TaskRow | WorkerRow | MessageRow | SafeguardStatus


def as_payload(row: PipelineRow) -> dict[str, Any]:
    """``row`` as a plain JSON object, for surfaces typed ``dict[str, Any]``.

    No copy: the row already is a plain dict. HTTP routes keep their
    ``dict[str, Any]`` return annotation (FastAPI derives the response model
    and the OpenAPI schema from it) and return rows through this.
    """
    return cast("dict[str, Any]", row)


def as_payloads(rows: Sequence[PipelineRow]) -> list[dict[str, Any]]:
    """List form of :func:`as_payload` (returns ``rows`` itself)."""
    return cast("list[dict[str, Any]]", rows)


__all__ = [
    "AuditEntry",
    "MessageRow",
    "PipelineRow",
    "SafeguardStatus",
    "TaskRow",
    "WorkerRow",
    "as_payload",
    "as_payloads",
    "message_from_row",
    "messages_from_rows",
    "task_from_row",
    "tasks_from_rows",
    "worker_from_row",
    "workers_from_rows",
]
