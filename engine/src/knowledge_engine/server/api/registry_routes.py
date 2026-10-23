"""Registry CRUD + lifecycle routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ...core.registry import RegistryEntry, RegistryKind
from ..deps import RegistryDep
from ..schemas import (
    EntryIn,
    LifecycleIn,
    LifecycleState,
    RegistryEntryOut,
    RemovedResponse,
    ToggleIn,
)

router = APIRouter()

__all__ = ["EntryIn", "LifecycleIn", "ToggleIn", "router"]


# Entries come from the operator-edited registry.json: every route emits exactly
# the fields a row has (``response_model_exclude_unset``) rather than padding
# missing ones with defaults.


@router.get("", response_model=list[RegistryEntryOut], response_model_exclude_unset=True)
def list_entries(
    registry: RegistryDep, kind: RegistryKind | None = None, enabled_only: bool = False
) -> list[dict[str, Any]]:
    return registry.list(kind=kind, enabled_only=enabled_only)


@router.get("/{entry_id}", response_model=RegistryEntryOut, response_model_exclude_unset=True)
def get_entry(entry_id: str, registry: RegistryDep) -> dict[str, Any]:
    entry = registry.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="not found")
    return entry


@router.post("", response_model=RegistryEntryOut, response_model_exclude_unset=True)
def upsert_entry(payload: EntryIn, registry: RegistryDep) -> dict[str, Any]:
    entry = RegistryEntry(**payload.model_dump())
    return registry.upsert(entry)


@router.patch(
    "/{entry_id}/toggle", response_model=RegistryEntryOut, response_model_exclude_unset=True
)
def toggle_entry(entry_id: str, payload: ToggleIn, registry: RegistryDep) -> dict[str, Any]:
    result = registry.set_enabled(entry_id, payload.enabled)
    if not result:
        raise HTTPException(status_code=404, detail="not found")
    return result


@router.delete("/{entry_id}", response_model=RemovedResponse)
def remove_entry(entry_id: str, registry: RegistryDep) -> RemovedResponse:
    ok = registry.remove(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return RemovedResponse(removed=entry_id)


@router.get("/lifecycle/state", response_model=LifecycleState, response_model_exclude_unset=True)
def get_lifecycle(registry: RegistryDep) -> dict[str, Any]:
    return registry.lifecycle()


@router.patch("/lifecycle/state", response_model=LifecycleState, response_model_exclude_unset=True)
def set_lifecycle(payload: LifecycleIn, registry: RegistryDep) -> dict[str, Any]:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    return registry.set_lifecycle(**updates)
