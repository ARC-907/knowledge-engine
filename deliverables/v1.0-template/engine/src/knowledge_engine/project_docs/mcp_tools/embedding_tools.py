"""Embedding MCP tools for project-docs.

Exposes the optional semantic-search subsystem to MCP clients on top of the
frozen :mod:`knowledge_engine.project_docs.embeddings` package
(``embeddings.providers`` for provider resolution + vector math,
``embeddings.index`` for generate / refresh / search / cluster).

Every tool is **conservative by default**:

- when embeddings are disabled in config, *all* tools return
  ``status_result("disabled")`` rather than raising;
- the read tools that surface scored records (``semantic_search`` /
  ``similar_records``) additionally require the ``mcp.allow_embedding_search``
  permission, returning ``status_result("not_permitted")`` otherwise;
- ``embedding_status`` is *always* safe to call and returns a useful status
  dict (enabled flag, provider, model, search permission, and — when enabled —
  the number of stored vectors) regardless of the gates, so an agent can
  discover the posture before attempting heavier calls.

Tool group: ``embedding``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..embeddings import index as emb_index
from ..embeddings import providers as emb_providers
from .base import ToolContext, status_result, text_result

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3

    from ..config import ProjectDocsConfig


GROUP = "embedding"


# --------------------------------------------------------------------------- #
# Tool definitions
# --------------------------------------------------------------------------- #
def tools(cfg: ProjectDocsConfig) -> list[dict]:
    """Return embedding-tool definitions.

    The tools are always advertised so an agent can discover them and read the
    ``embedding_status`` posture; the gates are enforced at dispatch time (a
    disabled / not-permitted call returns a structured status, not an error).
    """
    project_prop = {
        "project_fp": {
            "type": "string",
            "description": "Project fingerprint (defaults to the active project).",
        }
    }
    return [
        {
            "name": "project_docs.embedding_status",
            "description": (
                "Report the embedding subsystem posture: whether it is enabled, "
                "the configured provider/model, whether semantic search is "
                "permitted, and how many vectors are indexed. Always safe to call."
            ),
            "inputSchema": {"type": "object", "properties": dict(project_prop)},
        },
        {
            "name": "project_docs.generate_embeddings",
            "description": (
                "Generate and store embeddings for project documents. Returns "
                "the number embedded. Returns status 'disabled' when embeddings "
                "are off."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "record_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific record_ids to embed (default: all).",
                    },
                },
            },
        },
        {
            "name": "project_docs.refresh_embeddings",
            "description": (
                "Re-generate embeddings for documents (optionally a subset by "
                "record_id), overwriting existing vectors. Returns the number "
                "refreshed. Returns status 'disabled' when embeddings are off."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "record_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific record_ids to refresh (default: all).",
                    },
                },
            },
        },
        {
            "name": "project_docs.semantic_search",
            "description": (
                "Search project documents by semantic similarity to a query. "
                "Requires embeddings enabled and the allow_embedding_search "
                "permission; otherwise returns a structured status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "query": {
                        "type": "string",
                        "description": "Natural-language query to match against documents.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default 10).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "project_docs.similar_records",
            "description": (
                "Find documents most similar to a given document by embedding "
                "cosine similarity. Requires embeddings enabled and the "
                "allow_embedding_search permission."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "record_id": {
                        "type": "string",
                        "description": "The record_id to find neighbours for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of neighbours (default 10).",
                    },
                },
                "required": ["record_id"],
            },
        },
        {
            "name": "project_docs.cluster_records",
            "description": (
                "Group embedded documents into clusters by similarity. Returns "
                "cluster membership. Returns status 'disabled' when embeddings "
                "are off."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **project_prop,
                    "k": {
                        "type": "integer",
                        "description": "Maximum number of clusters (default 5).",
                    },
                },
            },
        },
    ]


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Dispatch an embedding tool call, enforcing config gates."""
    args = args or {}
    if name == "project_docs.embedding_status":
        return _embedding_status(args, ctx)
    if name == "project_docs.generate_embeddings":
        return _generate_embeddings(args, ctx)
    if name == "project_docs.refresh_embeddings":
        return _refresh_embeddings(args, ctx)
    if name == "project_docs.semantic_search":
        return _semantic_search(args, ctx)
    if name == "project_docs.similar_records":
        return _similar_records(args, ctx)
    if name == "project_docs.cluster_records":
        return _cluster_records(args, ctx)
    return status_result("unknown_tool", name=name)


# --------------------------------------------------------------------------- #
# Gate / resolution helpers
# --------------------------------------------------------------------------- #
def _enabled(ctx: ToolContext) -> bool:
    """Return True when the embeddings subsystem is enabled in config."""
    return bool(getattr(ctx.cfg.embeddings, "enabled", False))


def _permitted(ctx: ToolContext) -> bool:
    """Return True when MCP embedding search is permitted in config."""
    return bool(getattr(ctx.cfg.mcp, "allow_embedding_search", False))


def _resolve_conn(args: dict[str, Any], ctx: ToolContext) -> sqlite3.Connection | None:
    """Return the project DB connection for the requested project (or None)."""
    return ctx.project_conn(project_fp=args.get("project_fp"))


def _provider(ctx: ToolContext) -> emb_providers.EmbeddingProvider | None:
    """Resolve the configured embedding provider (None when unavailable)."""
    return emb_providers.get_provider(ctx.cfg)


def _stored_count(conn: sqlite3.Connection) -> int:
    """Return the total number of stored embedding vectors."""
    row = conn.execute("SELECT COUNT(*) AS c FROM doc_embeddings").fetchone()
    return int(row["c"]) if row is not None else 0


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def _embedding_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the embedding posture; always safe regardless of gates."""
    emb = ctx.cfg.embeddings
    payload: dict[str, Any] = {
        "status": "ok",
        "enabled": bool(getattr(emb, "enabled", False)),
        "provider": getattr(emb, "provider", "none"),
        "model": getattr(emb, "model", ""),
        "search_permitted": _permitted(ctx),
    }
    if payload["enabled"]:
        conn = _resolve_conn(args, ctx)
        if conn is None:
            payload["project"] = "unknown"
            payload["indexed"] = 0
        else:
            payload["indexed"] = _stored_count(conn)
    return text_result(payload)


def _generate_embeddings(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Embed documents; gated on embeddings.enabled."""
    if not _enabled(ctx):
        return status_result("disabled")
    conn = _resolve_conn(args, ctx)
    if conn is None:
        return status_result("unknown_project")
    provider = _provider(ctx)
    if provider is None:
        return status_result("not_configured")
    embedded = emb_index.generate(conn, provider, record_ids=args.get("record_ids"))
    return status_result("ok", embedded=embedded, total=_stored_count(conn))


def _refresh_embeddings(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Re-embed documents; gated on embeddings.enabled."""
    if not _enabled(ctx):
        return status_result("disabled")
    conn = _resolve_conn(args, ctx)
    if conn is None:
        return status_result("unknown_project")
    provider = _provider(ctx)
    if provider is None:
        return status_result("not_configured")
    refreshed = emb_index.refresh(conn, provider, record_ids=args.get("record_ids"))
    return status_result("ok", refreshed=refreshed, total=_stored_count(conn))


def _semantic_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Semantic search; gated on enabled + allow_embedding_search."""
    if not _enabled(ctx):
        return status_result("disabled")
    if not _permitted(ctx):
        return status_result("not_permitted")
    query = (args.get("query") or "").strip()
    if not query:
        return status_result("invalid_args", reason="missing query")
    conn = _resolve_conn(args, ctx)
    if conn is None:
        return status_result("unknown_project")
    provider = _provider(ctx)
    if provider is None:
        return status_result("not_configured")
    limit = int(args.get("limit") or 10)
    results = emb_index.semantic_search(conn, provider, query, limit=limit)
    return text_result(
        {"status": "ok", "query": query, "count": len(results), "results": results}
    )


def _similar_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Nearest neighbours for a record; gated on enabled + permission."""
    if not _enabled(ctx):
        return status_result("disabled")
    if not _permitted(ctx):
        return status_result("not_permitted")
    record_id = (args.get("record_id") or "").strip()
    if not record_id:
        return status_result("invalid_args", reason="missing record_id")
    conn = _resolve_conn(args, ctx)
    if conn is None:
        return status_result("unknown_project")
    provider = _provider(ctx)
    if provider is None:
        return status_result("not_configured")
    limit = int(args.get("limit") or 10)
    results = emb_index.similar_records(conn, provider, record_id, limit=limit)
    return text_result(
        {
            "status": "ok",
            "record_id": record_id,
            "count": len(results),
            "results": results,
        }
    )


def _cluster_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Cluster stored vectors; gated on embeddings.enabled."""
    if not _enabled(ctx):
        return status_result("disabled")
    conn = _resolve_conn(args, ctx)
    if conn is None:
        return status_result("unknown_project")
    k = int(args.get("k") or 5)
    clusters = emb_index.cluster_records(conn, k=k)
    return text_result(
        {"status": "ok", "k": k, "count": len(clusters), "clusters": clusters}
    )
