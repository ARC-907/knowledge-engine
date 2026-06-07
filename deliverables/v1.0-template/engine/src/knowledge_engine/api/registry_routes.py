"""Registry CRUD + lifecycle routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..registry import RegistryEntry

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
def list_entries(request: Request, kind: str | None = None, enabled_only: bool = False) -> list[dict]:
    return request.app.state.registry.list(kind=kind, enabled_only=enabled_only)


@router.get("/{entry_id}")
def get_entry(entry_id: str, request: Request) -> dict:
    entry = request.app.state.registry.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="not found")
    return entry


@router.post("")
def upsert_entry(payload: EntryIn, request: Request) -> dict:
    entry = RegistryEntry(**payload.model_dump())
    return request.app.state.registry.upsert(entry)


@router.patch("/{entry_id}/toggle")
def toggle_entry(entry_id: str, payload: ToggleIn, request: Request) -> dict:
    result = request.app.state.registry.set_enabled(entry_id, payload.enabled)
    if not result:
        raise HTTPException(status_code=404, detail="not found")
    return result


@router.delete("/{entry_id}")
def remove_entry(entry_id: str, request: Request) -> dict:
    ok = request.app.state.registry.remove(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"removed": entry_id}


@router.get("/lifecycle/state")
def get_lifecycle(request: Request) -> dict:
    return request.app.state.registry.lifecycle()


@router.patch("/lifecycle/state")
def set_lifecycle(payload: LifecycleIn, request: Request) -> dict:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    return request.app.state.registry.set_lifecycle(**updates)
