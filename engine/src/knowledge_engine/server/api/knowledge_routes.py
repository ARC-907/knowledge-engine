"""Scoped-knowledge admin routes under ``/knowledge`` (local-trust, like ``/registry``).

Every route validates its input through the same operation catalog as the
``knowledge_*`` MCP tools and the ``knowledge-engine knowledge`` CLI
(:mod:`knowledge_engine.knowledge.service`), so the three surfaces return
the same JSON. Failures are typed
:class:`~knowledge_engine.knowledge.errors.KnowledgeError` answers:
``{"detail": message, "type": code, "error": {"code", "message", "details",
"retryable"}}`` with the code's HTTP status. The legacy ``/search`` route is
unchanged by this surface.

``POST /knowledge/search`` and ``GET``/``POST /knowledge/open`` bind the
retrieval broker (:mod:`knowledge_engine.knowledge.broker`) through the same
request models as the ``knowledge.search`` / ``knowledge.open`` MCP tools and
``knowledge-engine knowledge search|open``
(:mod:`knowledge_engine.knowledge.broker_api`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, ConfigDict, Field

from ...knowledge.broker_api import (
    KnowledgeSearchEnvelope,
    OpenRequest,
    SearchRequest,
    run_open,
    run_search,
)
from ...knowledge.contracts import (
    Collection,
    Description,
    DisplayName,
    OpenResult,
    Project,
    ScopeProfile,
    SourceRecord,
    Workspace,
)
from ...knowledge.evidence.citations import (
    CitationExportRequest,
    CitationExportResult,
    CitationRequest,
    ValidationReport,
)
from ...knowledge.service import (
    ClassificationPatch,
    CollectionList,
    EvidenceOpenInput,
    EvidenceOpenResult,
    EvidenceRevisions,
    KnowledgeStatus,
    MembershipResult,
    ProfileList,
    ProjectContextLink,
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
from ..deps import BrokerDep, KnowledgeDep

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


class ProjectContextLinkBody(_Body):
    root: str = Field(description="Absolute path of the project-context project root.")
    project_fp: str | None = Field(
        default=None, description="Project fingerprint; defaults to the one registered for root."
    )


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


@router.put(
    "/workspaces/{workspace_id}/project-context/links/{project_id}",
    response_model=ProjectContextLink,
)
def link_project_context(
    workspace_id: str, project_id: str, payload: ProjectContextLinkBody, service: KnowledgeDep
) -> dict[str, Any]:
    args = {
        "workspace_id": workspace_id,
        "project_id": project_id,
        **_drop_none(payload.model_dump()),
    }
    return invoke(service, "project_context_link", args)


@router.delete(
    "/workspaces/{workspace_id}/project-context/links/{project_id}",
    response_model=ProjectContextLink,
)
def unlink_project_context(
    workspace_id: str, project_id: str, service: KnowledgeDep
) -> dict[str, Any]:
    args = {"workspace_id": workspace_id, "project_id": project_id}
    return invoke(service, "project_context_unlink", args)


# ── retrieval broker ─────────────────────────────────────────────────


@router.post("/search", response_model=KnowledgeSearchEnvelope)
def broker_search(payload: SearchRequest, broker: BrokerDep) -> dict[str, Any]:
    """Scoped search across the workspace's sources (see KNOWLEDGE-SEARCH.md)."""
    return run_search(broker, payload.model_dump())


@router.post("/open", response_model=OpenResult)
def broker_open(payload: OpenRequest, broker: BrokerDep) -> dict[str, Any]:
    """Open one in-scope record at an exact revision (the current one when omitted)."""
    return run_open(broker, payload.model_dump(mode="json", exclude_none=True))


@router.get("/open", response_model=OpenResult)
def broker_open_get(
    broker: BrokerDep,
    workspace_id: Annotated[str, Query(description="Workspace of the scope.")],
    source_id: str,
    record_id: str,
    revision_id: Annotated[
        str | None, Query(description="Exact revision; omit for the current one.")
    ] = None,
    project_id: str | None = None,
    profile_id: str | None = None,
    include_workspace: bool = False,
) -> dict[str, Any]:
    """``POST /knowledge/open`` with the scope in query parameters (no locator)."""
    context = _drop_none(
        {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "include_workspace": include_workspace or None,
        }
    )
    return run_open(
        broker,
        _drop_none(
            {
                "context": context,
                "source_id": source_id,
                "record_id": record_id,
                "revision_id": revision_id,
            }
        ),
    )


# ── Evidence and citations (V2 P3/P4) ────────────────────────────────


@router.post("/evidence/open", response_model=EvidenceOpenResult)
def open_evidence(payload: EvidenceOpenInput, service: KnowledgeDep) -> dict[str, Any]:
    """Open the exact cited revision (scope rechecked); never follows to the latest one."""
    return invoke(service, "evidence_open", payload.model_dump(mode="json", exclude_none=True))


@router.get("/records/{source_id}/{record_id}/revisions", response_model=EvidenceRevisions)
def list_revisions(source_id: str, record_id: str, service: KnowledgeDep) -> dict[str, Any]:
    return invoke(service, "evidence_revisions", {"source_id": source_id, "record_id": record_id})


@router.post("/citations/validate", response_model=ValidationReport)
def validate_citations(payload: CitationRequest, service: KnowledgeDep) -> dict[str, Any]:
    """Validate a claim-to-evidence map (not proof of entailment)."""
    return invoke(service, "cite_validate", payload.model_dump(mode="json", exclude_none=True))


@router.post("/citations/export", response_model=CitationExportResult)
def export_citations(payload: CitationExportRequest, service: KnowledgeDep) -> dict[str, Any]:
    """Validate and export Markdown references and/or CSL-JSON (scope rechecked)."""
    return invoke(service, "cite_export", payload.model_dump(mode="json", exclude_none=True))


__all__ = ["router"]
