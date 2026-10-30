"""Admin operations over the metadata store, shared by the HTTP, MCP and CLI bindings.

Each operation has one typed input model and one handler; the bindings only
translate transport details. :data:`OPERATIONS` is the catalog the MCP tool
group is generated from (tool name ``knowledge_<operation>``), so the three
surfaces cannot drift apart. Like the registry routes, these operations are
local-trust: a deployment that exposes them must put an authenticating
boundary in front.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import (
    Collection,
    CollectionId,
    CurationStatus,
    Description,
    DisplayName,
    EffectiveScope,
    Project,
    ProjectId,
    RecordId,
    ResearchType,
    ScopeProfile,
    Source,
    SourceId,
    SourceRecord,
    Workspace,
    WorkspaceId,
    parse_query_context,
    validation_messages,
)
from .errors import InvalidInputError
from .store import MAX_PAGE, UNSET, KnowledgeStore

_logger = logging.getLogger(__name__)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoInput(_Input):
    """No arguments."""


class WorkspaceCreate(_Input):
    display_name: DisplayName
    workspace_id: WorkspaceId | None = Field(default=None, description="Generated when omitted.")
    description: Description = ""


class WorkspaceRef(_Input):
    workspace_id: WorkspaceId


class ProjectCreate(_Input):
    workspace_id: WorkspaceId
    display_name: DisplayName = Field(description="Need not be unique; the project id is.")
    project_id: ProjectId | None = Field(default=None, description="Generated when omitted.")
    description: Description = ""


class CollectionCreate(_Input):
    workspace_id: WorkspaceId
    display_name: DisplayName
    collection_id: CollectionId | None = Field(default=None, description="Generated when omitted.")
    project_id: ProjectId | None = Field(
        default=None, description="Owning project; omit for a workspace-wide collection."
    )
    description: Description = ""


class CollectionListInput(_Input):
    workspace_id: WorkspaceId
    project_id: ProjectId | None = None


class RecordRefIn(_Input):
    source_id: SourceId
    record_id: RecordId


class MembershipInput(_Input):
    collection_id: CollectionId
    records: list[RecordRefIn] = Field(min_length=1, max_length=MAX_PAGE)


class ClassifyInput(_Input):
    source_id: SourceId
    record_id: RecordId
    research_type: ResearchType | None = None
    curation_status: CurationStatus | None = None
    tags: list[str] | None = Field(default=None, description="Replace the tag set.")
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    project_id: ProjectId | None = Field(default=None, description="Assign to this project.")
    clear_project: bool = Field(
        default=False, description="Make the record workspace-wide again (no project)."
    )


class ClassificationPatch(_Input):
    """The HTTP body of a classification change (the record comes from the path)."""

    research_type: ResearchType | None = None
    curation_status: CurationStatus | None = None
    tags: list[str] | None = Field(default=None, description="Replace the tag set.")
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    project_id: ProjectId | None = Field(default=None, description="Assign to this project.")
    clear_project: bool = False


class RecordQueryInput(_Input):
    context: dict[str, Any] = Field(
        description="A QueryContext (workspace_id required; see the knowledge contracts)."
    )
    limit: int = Field(default=100, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)


class ProfileInput(_Input):
    profile: dict[str, Any] = Field(description="A ScopeProfile.")


# ── Outputs ──────────────────────────────────────────────────────────


class WorkspaceList(BaseModel):
    workspaces: list[Workspace]


class ProjectList(BaseModel):
    projects: list[Project]


class SourceList(BaseModel):
    sources: list[Source]


class CollectionList(BaseModel):
    collections: list[Collection]


class MembershipResult(BaseModel):
    collection_id: str
    added: int = 0
    removed: int = 0
    unchanged: int = 0


class RecordPage(BaseModel):
    effective_scope: EffectiveScope
    records: list[SourceRecord]
    total: int
    limit: int
    offset: int


class ProfileList(BaseModel):
    profiles: list[ScopeProfile]


class StatusCounts(BaseModel):
    workspaces: int
    projects: int
    sources: int
    collections: int
    records: int
    collection_memberships: int


class KnowledgeStatus(BaseModel):
    schema_version: int
    latest_schema_version: int
    counts: StatusCounts
    records_by_research_type: dict[str, int]
    records_by_curation_status: dict[str, int]
    last_backfill: dict[str, Any] | None = None


# ── Service ──────────────────────────────────────────────────────────


class KnowledgeService:
    """The admin operations around one store; every method returns a model."""

    def __init__(self, store: KnowledgeStore, *, profile: str = "default") -> None:
        self.store = store
        self.profile = profile

    def status(self, _: NoInput | None = None) -> KnowledgeStatus:
        return KnowledgeStatus.model_validate(self.store.status())

    def workspace_create(self, data: WorkspaceCreate) -> Workspace:
        return self.store.create_workspace(
            data.display_name, workspace_id=data.workspace_id, description=data.description
        )

    def workspace_list(self, _: NoInput | None = None) -> WorkspaceList:
        return WorkspaceList(workspaces=self.store.list_workspaces())

    def project_create(self, data: ProjectCreate) -> Project:
        return self.store.create_project(
            data.workspace_id,
            data.display_name,
            project_id=data.project_id,
            description=data.description,
        )

    def project_list(self, data: WorkspaceRef) -> ProjectList:
        return ProjectList(projects=self.store.list_projects(data.workspace_id))

    def source_list(self, data: WorkspaceRef) -> SourceList:
        return SourceList(sources=self.store.list_sources(data.workspace_id))

    def collection_create(self, data: CollectionCreate) -> Collection:
        return self.store.create_collection(
            data.workspace_id,
            data.display_name,
            collection_id=data.collection_id,
            project_id=data.project_id,
            description=data.description,
        )

    def collection_list(self, data: CollectionListInput) -> CollectionList:
        return CollectionList(
            collections=self.store.list_collections(data.workspace_id, project_id=data.project_id)
        )

    def collection_add(self, data: MembershipInput) -> MembershipResult:
        refs = [(ref.source_id, ref.record_id) for ref in data.records]
        result = self.store.add_members(data.collection_id, refs)
        return MembershipResult(collection_id=data.collection_id, **result)

    def collection_remove(self, data: MembershipInput) -> MembershipResult:
        refs = [(ref.source_id, ref.record_id) for ref in data.records]
        result = self.store.remove_members(data.collection_id, refs)
        return MembershipResult(collection_id=data.collection_id, **result)

    def record_classify(self, data: ClassifyInput) -> SourceRecord:
        if data.clear_project and data.project_id is not None:
            raise InvalidInputError("pass either project_id or clear_project, not both")
        project: Any = UNSET
        if data.clear_project:
            project = None
        elif data.project_id is not None:
            project = data.project_id
        return self.store.classify(
            data.source_id,
            data.record_id,
            research_type=data.research_type.value if data.research_type else None,
            curation_status=data.curation_status.value if data.curation_status else None,
            tags=data.tags,
            add_tags=data.add_tags,
            remove_tags=data.remove_tags,
            project_id=project,
        )

    def record_query(self, data: RecordQueryInput) -> RecordPage:
        context = parse_query_context(data.context)
        scope, records, total = self.store.query_records(
            context, configured_profile=self.profile, limit=data.limit, offset=data.offset
        )
        return RecordPage(
            effective_scope=scope,
            records=records,
            total=total,
            limit=data.limit,
            offset=data.offset,
        )

    def profile_set(self, data: ProfileInput) -> ScopeProfile:
        try:
            profile = ScopeProfile.model_validate(data.profile)
        except ValidationError as exc:
            raise InvalidInputError(
                "invalid scope profile", details={"errors": validation_messages(exc)}
            ) from exc
        return self.store.set_profile(profile)

    def profile_list(self, data: WorkspaceRef) -> ProfileList:
        return ProfileList(profiles=self.store.list_profiles(data.workspace_id))


@dataclass(frozen=True)
class Operation:
    """One admin operation: its input model, description and service method name."""

    name: str
    description: str
    input_model: type[BaseModel]
    method: str
    read_only: bool


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "status",
        "Counts of workspaces, projects, sources, collections and records, the schema "
        "version and the last backfill run of the scoped-knowledge store.",
        NoInput,
        "status",
        True,
    ),
    Operation("workspace_list", "List knowledge workspaces.", NoInput, "workspace_list", True),
    Operation(
        "workspace_create",
        "Create a knowledge workspace (an explicit knowledge boundary with a stable id).",
        WorkspaceCreate,
        "workspace_create",
        False,
    ),
    Operation(
        "project_list", "List the projects of a workspace.", WorkspaceRef, "project_list", True
    ),
    Operation(
        "project_create",
        "Create a project in a workspace. Display names may repeat; the returned "
        "project_id is the identity.",
        ProjectCreate,
        "project_create",
        False,
    ),
    Operation(
        "source_list",
        "List the record-owning sources registered in a workspace.",
        WorkspaceRef,
        "source_list",
        True,
    ),
    Operation(
        "collection_list",
        "List the collections of a workspace (optionally of one project).",
        CollectionListInput,
        "collection_list",
        True,
    ),
    Operation(
        "collection_create",
        "Create a collection (curated grouping; organization, not permission).",
        CollectionCreate,
        "collection_create",
        False,
    ),
    Operation(
        "collection_add",
        "Add records to a collection; a record may belong to several collections.",
        MembershipInput,
        "collection_add",
        False,
    ),
    Operation(
        "collection_remove",
        "Remove records from a collection.",
        MembershipInput,
        "collection_remove",
        False,
    ),
    Operation(
        "record_classify",
        "Set a record's research type, curation status, tags or project. Omitted fields "
        "are unchanged; nothing is inferred.",
        ClassifyInput,
        "record_classify",
        False,
    ),
    Operation(
        "record_query",
        "List the records in a scope. Omitted selections use the configured profile; an "
        "explicit empty selection matches nothing; unknown ids are errors; tags are ANDed, "
        "values within a list ORed.",
        RecordQueryInput,
        "record_query",
        True,
    ),
    Operation(
        "profile_list",
        "List the scope profiles of a workspace.",
        WorkspaceRef,
        "profile_list",
        True,
    ),
    Operation(
        "profile_set",
        "Create or replace a named scope profile (the defaults for omitted selections).",
        ProfileInput,
        "profile_set",
        False,
    ),
)

OPERATIONS_BY_NAME: dict[str, Operation] = {op.name: op for op in OPERATIONS}


def invoke(service: KnowledgeService, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate ``arguments`` for operation ``name``, run it and return JSON-safe output."""
    operation = OPERATIONS_BY_NAME.get(name)
    if operation is None:
        raise InvalidInputError(f"unknown knowledge operation {name!r}")
    try:
        data = operation.input_model.model_validate(arguments)
    except ValidationError as exc:
        raise InvalidInputError(
            f"invalid arguments for {name}", details={"errors": validation_messages(exc)}
        ) from exc
    method: Callable[[Any], BaseModel] = getattr(service, operation.method)
    result = method(data)
    return result.model_dump(mode="json")


__all__ = [
    "OPERATIONS",
    "OPERATIONS_BY_NAME",
    "ClassificationPatch",
    "ClassifyInput",
    "CollectionCreate",
    "CollectionList",
    "CollectionListInput",
    "KnowledgeService",
    "KnowledgeStatus",
    "MembershipInput",
    "MembershipResult",
    "NoInput",
    "Operation",
    "ProfileInput",
    "ProfileList",
    "ProjectCreate",
    "ProjectList",
    "RecordPage",
    "RecordQueryInput",
    "RecordRefIn",
    "SourceList",
    "WorkspaceCreate",
    "WorkspaceList",
    "WorkspaceRef",
    "invoke",
]
