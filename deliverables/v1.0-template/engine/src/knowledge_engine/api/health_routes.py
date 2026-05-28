"""Health and metadata routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/info")
def info(request: Request) -> dict:
    config = request.app.state.config
    registry = request.app.state.registry
    providers = request.app.state.providers
    return {
        "version": __version__,
        "corpus_root": str(config.corpus_root),
        "data_dir": str(config.data_dir),
        "counts": {
            "libraries": len(registry.list("library")),
            "skills": len(registry.list("skill")),
            "tools": len(registry.list("tool")),
        },
        "providers": providers.list(),
        "lifecycle": registry.lifecycle(),
    }
