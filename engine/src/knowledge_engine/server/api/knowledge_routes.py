"""Scoped-knowledge admin routes under ``/knowledge`` (local-trust, like ``/registry``).

Every route validates its input through the same operation catalog as the
``knowledge_*`` MCP tools and the ``knowledge-engine knowledge`` CLI
(:mod:`knowledge_engine.knowledge.service`), so the three surfaces return
the same JSON. Failures are typed
:class:`~knowledge_engine.knowledge.errors.KnowledgeError` answers:
``{"detail": message, "type": code, "error": {"code", "message", "details",
"retryable"}}`` with the code's HTTP status. The legacy ``/search`` route is
unchanged by this surface.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field

from ...knowledge.contracts import (
    Collection,
    Description,
    DisplayName,
    Project,
    ScopeProfile,
    SourceRecord,
    Workspace,
)
from ...knowledge.service import (
    ClassificationPatch,
    CollectionList,
    KnowledgeStatus,
    MembershipResult,
    ProfileList,
    ProjectList,
    RecordPage,
    RecordQueryInput,
    RecordRefIn,
    SourceList,
    WorkspaceCreate,
    WorkspaceList,
    invoke,
)
from ...knowledge.store import MAX_PAGE
from ..deps import KnowledgeDep

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectBody(_Body):
    display_name: DisplayName
    project_id: str | None = Field(default=None, description="Generated when omitted.")
    description: Description = ""


class CollectionBody(_Body):
    display_name: DisplayName
    collection_id: str | None = Field(default=None, description="Generated when omitted.")
    project_id: str | None = None
    description: Description = ""


class MembersBody(_Body):
    records: list[RecordRefIn] = Field(min_length=1, max_length=MAX_PAGE)


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


@router.get("/status", response_model=KnowledgeStatus)
def status(service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "status", {})


@router.get("/workspaces", response_model=WorkspaceList)
def list_workspaces(service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "workspace_list", {})


@router.post("/workspaces", response_model=Workspace)
def create_workspace(payload: WorkspaceCreate, service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "workspace_create", payload.model_dump(exclude_none=True))


@router.get("/workspaces/{workspace_id}/projects", response_model=ProjectList)
def list_projects(workspace_id: str, service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "project_list", {"workspace_id": workspace_id})


@router.post("/workspaces/{workspace_id}/projects", response_model=Project)
def create_project(
    workspace_id: str, payload: ProjectBody, service: KnowledgeDep
) -> dict[str, Any]:
    args = {"workspace_id": workspace_id, **_drop_none(payload.model_dump())}
    return invoke(service, "project_create", args)


@router.get("/workspaces/{workspace_id}/sources", response_model=SourceList)
def list_sources(workspace_id: str, service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "source_list", {"workspace_id": workspace_id})


@router.get("/workspaces/{workspace_id}/collections", response_model=CollectionList)
def list_collections(
    workspace_id: str, service: KnowledgeDep, project_id: str | None = None
) -> dict[str, Any]:
    return invoke(
        service,
        "collection_list",
        _drop_none({"workspace_id": workspace_id, "project_id": project_id}),
    )


@router.post("/workspaces/{workspace_id}/collections", response_model=Collection)
def create_collection(
    workspace_id: str, payload: CollectionBody, service: KnowledgeDep
) -> dict[str, Any]:
    args = {"workspace_id": workspace_id, **_drop_none(payload.model_dump())}
    return invoke(service, "collection_create", args)


@router.post("/collections/{collection_id}/members", response_model=MembershipResult)
def add_members(collection_id: str, payload: MembersBody, service: KnowledgeDep) -> dict[str, Any]:
    args = {"collection_id": collection_id, **payload.model_dump()}
    return invoke(service, "collection_add", args)


@router.post("/collections/{collection_id}/members/remove", response_model=MembershipResult)
def remove_members(
    collection_id: str, payload: MembersBody, service: KnowledgeDep
) -> dict[str, Any]:
    args = {"collection_id": collection_id, **payload.model_dump()}
    return invoke(service, "collection_remove", args)


@router.patch("/records/{source_id}/{record_id}", response_model=SourceRecord)
def classify_record(
    source_id: str, record_id: str, payload: ClassificationPatch, service: KnowledgeDep
) -> dict[str, Any]:
    args = {
        "source_id": source_id,
        "record_id": record_id,
        **payload.model_dump(exclude_none=True, mode="json"),
    }
    return invoke(service, "record_classify", args)


@router.post("/records/query", response_model=RecordPage)
def query_records(payload: RecordQueryInput, service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "record_query", payload.model_dump())


@router.get("/workspaces/{workspace_id}/profiles", response_model=ProfileList)
def list_profiles(workspace_id: str, service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "profile_list", {"workspace_id": workspace_id})


@router.put("/workspaces/{workspace_id}/profiles/{profile_id}", response_model=ScopeProfile)
def set_profile(
    workspace_id: str,
    profile_id: str,
    service: KnowledgeDep,
    payload: Annotated[dict[str, Any], Body(description="ScopeProfile selections")],
) -> dict[str, Any]:
    profile = {**payload, "workspace_id": workspace_id, "profile_id": profile_id}
    return invoke(service, "profile_set", {"profile": profile})


__all__ = ["router"]
