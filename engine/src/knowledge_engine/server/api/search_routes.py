"""Search + reindex routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...retrieval.hybrid import DEFAULT_MODE, MAX_SEARCH_LIMIT, SearchMode
from ..deps import IndexerDep, SearcherDep
from ..schemas import EmbeddingErrorResponse, ReindexResponse, SearchHit, SearchResponse

router = APIRouter()

_EMBEDDING_ERRORS: dict[int | str, dict[str, object]] = {
    409: {
        "model": EmbeddingErrorResponse,
        "description": "`vector`/`hybrid`: the query model differs from the index's model.",
    },
    502: {
        "model": EmbeddingErrorResponse,
        "description": "`vector`/`hybrid`: the embedding provider failed.",
    },
    503: {
        "model": EmbeddingErrorResponse,
        "description": (
            "`vector`/`hybrid`: no embedding index, or vector search not configured "
            "(also answered while the engine is not started)."
        ),
    },
}


@router.get(
    "",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    responses=_EMBEDDING_ERRORS,
)
def search(
    searcher: SearcherDep,
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=MAX_SEARCH_LIMIT),
    mode: Annotated[
        SearchMode,
        Query(
            description=(
                "`fts` (default): FTS5 keyword search. `vector`: the corpus embedding index "
                "(`knowledge-engine embeddings build`). `hybrid`: both, merged by reciprocal "
                "rank fusion (k=60)."
            ),
        ),
    ] = DEFAULT_MODE,
) -> SearchResponse:
    results = searcher.search(q, mode=mode, limit=limit)
    return SearchResponse(
        query=q,
        mode=mode,
        count=len(results),
        results=[SearchHit.model_validate(hit) for hit in results],
        syntax_fallback=True if getattr(results, "syntax_fallback", False) else None,
    )


@router.post("/reindex", response_model=ReindexResponse)
def reindex(indexer: IndexerDep) -> ReindexResponse:
    counts = indexer.rebuild()
    return ReindexResponse.model_validate({"status": "ok", **counts})
