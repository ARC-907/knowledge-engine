"""Pointer URI grammar, allocation, resolution, and back-references.

A *pointer* is a stable, compact URI that names a stored project-docs record so
that a source file (e.g. a docstring) can be replaced by a reference instead of
carrying the full text. The canonical grammar is::

    ke-doc://<type>/project/<project_fp>/branch/<branch_fp>/<kind>/<record_id>

``<type>`` is one of :data:`knowledge_engine.project_docs.schema.POINTER_TYPES`.
``KE-DOCSTRING://`` is a recognized alias for the docstring profile; it maps to
``type=docstring`` and uses the four-segment short form::

    KE-DOCSTRING://project/<project_fp>/branch/<branch_fp>/doc/<record_id>

Resolution is *compact by default*: :func:`resolve` returns a summary envelope
and only includes full record content when the caller passes a config whose
``mcp.allow_full_content`` gate is enabled. Nothing here mutates source files —
that lives in the guarded scanner apply path.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .schema import (
    POINTER_DOC,
    POINTER_DOCSTRING,
    POINTER_TYPES,
    RESULT_FULL,
)

#: Canonical scheme for the general pointer grammar.
KE_DOC_SCHEME = "ke-doc"

#: Alias scheme that maps to the docstring pointer profile.
DOCSTRING_ALIAS_SCHEME = "ke-docstring"

#: Default ``<kind>`` segment per pointer type. The docstring profile uses
#: ``doc`` (per the design spec's alias form); other types reuse their type name.
_KIND_BY_TYPE: dict[str, str] = {
    POINTER_DOCSTRING: POINTER_DOC,
}


def _kind_for(ptype: str) -> str:
    """Return the ``<kind>`` segment used for a given pointer type."""
    return _KIND_BY_TYPE.get(ptype, ptype)


def format_pointer(ptype: str, project_fp: str, branch_fp: str, record_id: str) -> str:
    """Build a canonical ``ke-doc://`` pointer URI.

    The same URI is used as the primary key of the ``doc_pointers`` row, so
    formatting is deterministic for a given ``(ptype, project_fp, branch_fp,
    record_id)`` tuple.
    """
    if ptype not in POINTER_TYPES:
        raise ValueError(f"unknown pointer type: {ptype!r}")
    kind = _kind_for(ptype)
    return (
        f"{KE_DOC_SCHEME}://{ptype}"
        f"/project/{project_fp}/branch/{branch_fp}/{kind}/{record_id}"
    )


def parse_pointer(uri: str) -> dict[str, str]:
    """Parse a pointer URI into its components.

    Accepts both the canonical ``ke-doc://`` form and the ``KE-DOCSTRING://``
    alias (scheme comparison is case-insensitive). Returns a dict with keys
    ``scheme, ptype, project_fp, branch_fp, kind, record_id``. Raises
    :class:`ValueError` on a grammatically invalid URI.
    """
    if not isinstance(uri, str) or "://" not in uri:
        raise ValueError(f"not a pointer URI: {uri!r}")

    raw_scheme, _, remainder = uri.partition("://")
    scheme = raw_scheme.lower()
    segments = [seg for seg in remainder.split("/") if seg != ""]

    if scheme == DOCSTRING_ALIAS_SCHEME:
        # Short form: project/<fp>/branch/<fp>/<kind>/<record_id>
        if (
            len(segments) != 6
            or segments[0] != "project"
            or segments[2] != "branch"
        ):
            raise ValueError(f"malformed KE-DOCSTRING pointer: {uri!r}")
        return {
            "scheme": KE_DOC_SCHEME,
            "ptype": POINTER_DOCSTRING,
            "project_fp": segments[1],
            "branch_fp": segments[3],
            "kind": segments[4],
            "record_id": segments[5],
        }

    if scheme == KE_DOC_SCHEME:
        # Full form: <type>/project/<fp>/branch/<fp>/<kind>/<record_id>
        if (
            len(segments) != 7
            or segments[1] != "project"
            or segments[3] != "branch"
        ):
            raise ValueError(f"malformed ke-doc pointer: {uri!r}")
        ptype = segments[0]
        if ptype not in POINTER_TYPES:
            raise ValueError(f"unknown pointer type in URI: {ptype!r}")
        return {
            "scheme": KE_DOC_SCHEME,
            "ptype": ptype,
            "project_fp": segments[2],
            "branch_fp": segments[4],
            "kind": segments[5],
            "record_id": segments[6],
        }

    raise ValueError(f"unsupported pointer scheme: {raw_scheme!r}")


def allocate(
    project_conn: sqlite3.Connection,
    record_id: str,
    ptype: str,
    project_fp: str,
    branch_fp: str,
    source_path: str | None,
    span: Any,
    content_hash: str,
) -> str:
    """Allocate (persist) a pointer for ``record_id`` and return its URI.

    Writes one ``doc_pointers`` row keyed by the formatted pointer URI. The
    source span is stored as JSON in ``source_span_json``. Re-allocating the
    same pointer is a no-op upsert keyed on the pointer id.
    """
    pointer_id = format_pointer(ptype, project_fp, branch_fp, record_id)
    span_json = json.dumps(span)
    project_conn.execute(
        """
        INSERT INTO doc_pointers (
            pointer_id, record_id, scheme, ptype, project_fp, branch_fp,
            source_path, source_span_json, content_hash, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'active')
        ON CONFLICT(pointer_id) DO UPDATE SET
            record_id        = excluded.record_id,
            scheme           = excluded.scheme,
            ptype            = excluded.ptype,
            project_fp       = excluded.project_fp,
            branch_fp        = excluded.branch_fp,
            source_path      = excluded.source_path,
            source_span_json = excluded.source_span_json,
            content_hash     = excluded.content_hash
        """,
        (
            pointer_id,
            record_id,
            KE_DOC_SCHEME,
            ptype,
            project_fp,
            branch_fp,
            source_path,
            span_json,
            content_hash,
        ),
    )
    project_conn.commit()
    return pointer_id


def _load_span(span_json: str | None) -> Any:
    """Decode a stored ``source_span_json`` value, tolerating bad/empty data."""
    if not span_json:
        return None
    try:
        return json.loads(span_json)
    except (TypeError, ValueError):
        return None


def resolve(
    project_conn: sqlite3.Connection,
    uri: str,
    *,
    mode: str = "summary",
    cfg: Any = None,
) -> dict[str, Any] | None:
    """Resolve a pointer URI to a compact record envelope.

    Returns ``None`` when the URI is grammatically valid but no pointer row
    exists. Raises :class:`ValueError` for a malformed URI. The envelope is a
    summary by default; the full searchable body is included only when ``mode``
    is ``full`` *and* ``cfg.mcp.allow_full_content`` is true.
    """
    parsed = parse_pointer(uri)
    pointer_id = format_pointer(
        parsed["ptype"], parsed["project_fp"], parsed["branch_fp"], parsed["record_id"]
    )

    prow = project_conn.execute(
        "SELECT * FROM doc_pointers WHERE pointer_id = ?", (pointer_id,)
    ).fetchone()
    if prow is None:
        return None

    record_id = prow["record_id"]
    drow = project_conn.execute(
        "SELECT * FROM project_docs WHERE record_id = ?", (record_id,)
    ).fetchone()

    envelope: dict[str, Any] = {
        "pointer_id": pointer_id,
        "ptype": parsed["ptype"],
        "record_id": record_id,
        "project_fp": prow["project_fp"],
        "branch_fp": prow["branch_fp"],
        "source_path": prow["source_path"],
        "source_span": _load_span(prow["source_span_json"]),
        "content_hash": prow["content_hash"],
        "summary": "",
        "git_commit": None,
        "ingestion_run_id": None,
        "sanitization_status": None,
        "related": [],
    }

    if drow is not None:
        keys = drow.keys()
        envelope["summary"] = drow["summary"] if "summary" in keys else ""
        envelope["git_commit"] = drow["git_commit"] if "git_commit" in keys else None
        envelope["ingestion_run_id"] = (
            drow["ingestion_run_id"] if "ingestion_run_id" in keys else None
        )
        envelope["sanitization_status"] = (
            drow["sanitization_status"] if "sanitization_status" in keys else None
        )
        envelope["related"] = _related_records(project_conn, record_id)

    allow_full = bool(cfg is not None and getattr(getattr(cfg, "mcp", None), "allow_full_content", False))
    if mode == RESULT_FULL and allow_full:
        body = project_conn.execute(
            "SELECT searchable_body, raw_body FROM project_doc_bodies WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if body is not None:
            envelope["content"] = body["searchable_body"]
            if body["raw_body"] is not None:
                envelope["raw_content"] = body["raw_body"]

    return envelope


def _related_records(project_conn: sqlite3.Connection, record_id: str) -> list[dict[str, str]]:
    """Return linked records for ``record_id`` from ``project_doc_links``."""
    rows = project_conn.execute(
        "SELECT dst_record_id, link_type FROM project_doc_links WHERE src_record_id = ?",
        (record_id,),
    ).fetchall()
    return [
        {"record_id": r["dst_record_id"], "link_type": r["link_type"]} for r in rows
    ]


def list_pointers(
    project_conn: sqlite3.Connection,
    *,
    record_id: str | None = None,
) -> list[dict[str, Any]]:
    """List pointer rows, optionally filtered to one ``record_id``."""
    if record_id is None:
        rows = project_conn.execute(
            "SELECT * FROM doc_pointers ORDER BY created_at, pointer_id"
        ).fetchall()
    else:
        rows = project_conn.execute(
            "SELECT * FROM doc_pointers WHERE record_id = ? ORDER BY created_at, pointer_id",
            (record_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def validate_pointer(project_conn: sqlite3.Connection, uri: str) -> dict[str, bool]:
    """Validate a pointer URI's grammar and existence.

    Returns ``{valid, exists, content_hash_match}``. ``valid`` reflects only the
    grammar; a syntactically correct URI for a record that was never allocated
    yields ``{valid: True, exists: False, content_hash_match: False}``.
    ``content_hash_match`` compares the pointer's stored hash to the current
    record's ``content_hash``.
    """
    result = {"valid": False, "exists": False, "content_hash_match": False}
    try:
        parsed = parse_pointer(uri)
    except ValueError:
        return result
    result["valid"] = True

    pointer_id = format_pointer(
        parsed["ptype"], parsed["project_fp"], parsed["branch_fp"], parsed["record_id"]
    )
    prow = project_conn.execute(
        "SELECT record_id, content_hash FROM doc_pointers WHERE pointer_id = ?",
        (pointer_id,),
    ).fetchone()
    if prow is None:
        return result
    result["exists"] = True

    drow = project_conn.execute(
        "SELECT content_hash FROM project_docs WHERE record_id = ?",
        (prow["record_id"],),
    ).fetchone()
    if drow is not None and drow["content_hash"] == prow["content_hash"]:
        result["content_hash_match"] = True
    return result


def pointer_backrefs(
    project_conn: sqlite3.Connection,
    pointer_id: str,
) -> list[dict[str, Any]]:
    """Return back-reference rows recorded for ``pointer_id``."""
    rows = project_conn.execute(
        "SELECT * FROM pointer_backrefs WHERE pointer_id = ? ORDER BY id",
        (pointer_id,),
    ).fetchall()
    return [dict(r) for r in rows]
