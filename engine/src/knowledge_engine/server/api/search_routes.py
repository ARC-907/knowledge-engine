"""Search + reindex routes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter()


@router.get("")
def search(request: Request, q: str = Query(..., min_length=1), limit: int = 20) -> dict:
    results = request.app.state.indexer.search(q, limit=limit)
    return {"query": q, "count": len(results), "results": results}


@router.post("/reindex")
def reindex(request: Request) -> dict:
    counts = request.app.state.indexer.rebuild()
    return {"status": "ok", **counts}
