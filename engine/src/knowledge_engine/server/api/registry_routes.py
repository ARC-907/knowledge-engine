"""Registry CRUD + lifecycle routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...core.registry import RegistryEntry

router = APIRouter()


class EntryIn(BaseModel):
    id: str
    kind: Literal["library", "skill", "kit", "tool"]
    name: str
    path: str
    enabled: bool = True
    auto_registered: bool = False
    description: str = ""
    tags: list[str] = []


class ToggleIn(BaseModel):
    enabled: bool


class LifecycleIn(BaseModel):
    watch_enabled: bool | None = None
    auto_register: bool | None = None


@router.get("")
def list_entries(
    request: Request, kind: str | None = None, enabled_only: bool = False
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = request.app.state.registry.list(
        kind=kind, enabled_only=enabled_only
    )
    return entries


@router.get("/{entry_id}")
def get_entry(entry_id: str, request: Request) -> dict[str, Any]:
    entry: dict[str, Any] | None = request.app.state.registry.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="not found")
    return entry


@router.post("")
def upsert_entry(payload: EntryIn, request: Request) -> dict[str, Any]:
    entry = RegistryEntry(**payload.model_dump())
    saved: dict[str, Any] = request.app.state.registry.upsert(entry)
    return saved


@router.patch("/{entry_id}/toggle")
def toggle_entry(entry_id: str, payload: ToggleIn, request: Request) -> dict[str, Any]:
    result: dict[str, Any] | None = request.app.state.registry.set_enabled(
        entry_id, payload.enabled
    )
    if not result:
        raise HTTPException(status_code=404, detail="not found")
    return result


@router.delete("/{entry_id}")
def remove_entry(entry_id: str, request: Request) -> dict[str, Any]:
    ok = request.app.state.registry.remove(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"removed": entry_id}


@router.get("/lifecycle/state")
def get_lifecycle(request: Request) -> dict[str, Any]:
    state: dict[str, Any] = request.app.state.registry.lifecycle()
    return state


@router.patch("/lifecycle/state")
def set_lifecycle(payload: LifecycleIn, request: Request) -> dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    state: dict[str, Any] = request.app.state.registry.set_lifecycle(**updates)
    return state
