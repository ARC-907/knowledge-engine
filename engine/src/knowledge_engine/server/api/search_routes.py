"""Search + reindex routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..deps import IndexerDep
from ..schemas import ReindexResponse, SearchHit, SearchResponse

router = APIRouter()

#: Upper bound of ``limit`` on ``GET /search``.
MAX_SEARCH_LIMIT = 100


@router.get("", response_model=SearchResponse)
def search(
    indexer: IndexerDep,
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=MAX_SEARCH_LIMIT),
) -> SearchResponse:
    results = indexer.search(q, limit=limit)
    return SearchResponse(
        query=q,
        count=len(results),
        results=[SearchHit.model_validate(hit) for hit in results],
    )


@router.post("/reindex", response_model=ReindexResponse)
def reindex(indexer: IndexerDep) -> ReindexResponse:
    counts = indexer.rebuild()
    return ReindexResponse.model_validate({"status": "ok", **counts})
