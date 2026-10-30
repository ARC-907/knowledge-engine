"""Admin operations over the metadata store, shared by the HTTP, MCP and CLI bindings.

Each operation has one typed input model and one handler; the bindings only
translate transport details. :data:`OPERATIONS` is the catalog the MCP tool
group is generated from (tool name ``knowledge_<operation>``), so the three
surfaces cannot drift apart. Like the registry routes, these operations are
local-trust: a deployment that exposes them must put an authenticating
boundary in front.
"""

from __future__ import annotations

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
    EvidenceId,
    Locator,
    OpenResult,
    Project,
    ProjectId,
    RecordId,
    ResearchType,
    Revision,
    RevisionId,
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
from .evidence.citations import (
    CitationExportRequest,
    CitationExportResult,
    CitationRequest,
    ValidationReport,
    export_citations,
    validate_claims,
)
from .evidence.policy import EvidencePolicy
from .evidence.resolve import open_evidence, open_span
from .evidence.store import EVIDENCE_SCHEMA_VERSION, EvidenceStore
from .store import MAX_PAGE, UNSET, KnowledgeStore


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


class ProjectContextLinkInput(_Input):
    workspace_id: WorkspaceId
    project_id: ProjectId
    root: str = Field(
        min_length=1,
        max_length=4096,
        description="Absolute path of the project-context project root on this host.",
    )
    project_fp: str | None = Field(
        default=None,
        max_length=80,
        description="Project fingerprint (proj_...); defaults to the one registered for root.",
    )


class ProjectContextUnlinkInput(_Input):
    workspace_id: WorkspaceId
    project_id: ProjectId


class EvidenceOpenInput(_Input):
    """Open the exact cited revision: a stored ``evidence_id``, or record + revision (+ locator)."""

    context: dict[str, Any] = Field(
        description="A QueryContext (workspace_id required); the record must be in its scope."
    )
    evidence_id: EvidenceId | None = None
    source_id: SourceId | None = None
    record_id: RecordId | None = None
    revision_id: RevisionId | None = None
    locator: Locator | None = Field(default=None, description="A typed locator (ADR 0007).")


class EvidenceRevisionsInput(_Input):
    source_id: SourceId
    record_id: RecordId


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


class ProjectContextLink(BaseModel):
    source_id: str
    workspace_id: str
    project_id: str
    project_fp: str | None = None
    linked: bool


class EvidenceOpenResult(BaseModel):
    """The P0 ``OpenResult`` plus why it is not verified and the evidence identity."""

    result: OpenResult
    reason: str | None = None
    evidence_id: str | None = None
    excerpt_digest: str | None = None
    current_revision_id: str | None = None
    notice: str = "Verification states describe evidence resolution, not the truth of any claim."


class RevisionEntry(BaseModel):
    revision: Revision
    current: bool
    deleted_at: str | None = None
    external_locator: str | None = None


class EvidenceRevisions(BaseModel):
    source_id: str
    record_id: str
    logical_path: str | None
    aliases: list[str]
    current_revision_id: str | None
    deleted_at: str | None
    revisions: list[RevisionEntry]


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
    evidence: dict[str, int] | None = Field(
        default=None,
        description="Evidence store counts (revisions, spans, tombstones, pending outbox); "
        "absent before schema version 2.",
    )


# ── Service ──────────────────────────────────────────────────────────


class KnowledgeService:
    """The admin operations around one store; every method returns a model."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        profile: str = "default",
        evidence_policy: EvidencePolicy | None = None,
    ) -> None:
        self.store = store
        self.profile = profile
        self.evidence = EvidenceStore(store, evidence_policy)

    def status(self, _: NoInput | None = None) -> KnowledgeStatus:
        status = self.store.status()
        if status["schema_version"] >= EVIDENCE_SCHEMA_VERSION:
            status["evidence"] = self.evidence.stats()
        return KnowledgeStatus.model_validate(status)

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

    def project_context_link(self, data: ProjectContextLinkInput) -> ProjectContextLink:
        from .sources.project_context import link_project  # noqa: PLC0415 - optional subsystem

        return ProjectContextLink.model_validate(
            link_project(
                self.store,
                data.workspace_id,
                data.project_id,
                data.root,
                project_fp=data.project_fp,
            )
        )

    def project_context_unlink(self, data: ProjectContextUnlinkInput) -> ProjectContextLink:
        from .sources.project_context import unlink_project  # noqa: PLC0415 - optional subsystem

        return ProjectContextLink.model_validate(
            unlink_project(self.store, data.workspace_id, data.project_id)
        )

    def evidence_open(self, data: EvidenceOpenInput) -> EvidenceOpenResult:
        context = parse_query_context(data.context)
        explicit = (data.source_id, data.record_id, data.revision_id)
        if data.evidence_id is not None:
            if any(v is not None for v in (*explicit, data.locator)):
                raise InvalidInputError("give either evidence_id or source/record/revision")
            res = open_span(
                self.evidence, context, data.evidence_id, configured_profile=self.profile
            )
        else:
            if any(v is None for v in explicit):
                raise InvalidInputError("source_id, record_id and revision_id are required")
            res = open_evidence(
                self.evidence,
                context,
                source_id=str(data.source_id),
                record_id=str(data.record_id),
                revision_id=str(data.revision_id),
                locator=data.locator,
                configured_profile=self.profile,
            )
        return EvidenceOpenResult(
            result=res.result,
            reason=res.reason,
            evidence_id=res.evidence_id,
            excerpt_digest=res.excerpt_digest,
            current_revision_id=res.current_revision_id,
        )

    def evidence_revisions(self, data: EvidenceRevisionsInput) -> EvidenceRevisions:
        with self.evidence.snapshot():
            state = self.evidence.record_state(data.source_id, data.record_id)
            infos = self.evidence.list_revisions(data.source_id, data.record_id)
        return EvidenceRevisions(
            source_id=data.source_id,
            record_id=data.record_id,
            logical_path=state.record.logical_path,
            aliases=list(state.record.aliases),
            current_revision_id=state.current_revision_id,
            deleted_at=state.deleted_at,
            revisions=[
                RevisionEntry(
                    revision=info.revision,
                    current=info.revision.revision_id == state.current_revision_id,
                    deleted_at=info.deleted_at,
                    external_locator=info.external_locator,
                )
                for info in infos
            ],
        )

    def cite_validate(self, data: CitationRequest) -> ValidationReport:
        return validate_claims(self.evidence, data, configured_profile=self.profile)

    def cite_export(self, data: CitationExportRequest) -> CitationExportResult:
        return export_citations(self.evidence, data, configured_profile=self.profile)


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
    Operation(
        "project_context_link",
        "Link a project to the project-context store at a root on this host, so the "
        "retrieval broker searches that project's documentation (source "
        "project_context:<workspace_id>). Only linked project-context projects are searchable.",
        ProjectContextLinkInput,
        "project_context_link",
        False,
    ),
    Operation(
        "project_context_unlink",
        "Remove a project's project-context link; its documentation stops being searchable.",
        ProjectContextUnlinkInput,
        "project_context_unlink",
        False,
    ),
    Operation(
        "evidence_open",
        "Open the exact cited revision of a record (by evidence_id, or source/record/"
        "revision plus an optional typed locator) after rechecking the scope. Returns "
        "verified, stale (a newer revision exists; the cited one is returned), unavailable "
        "(bytes deleted or not retained), ambiguous or unsupported -- never the latest "
        "revision silently.",
        EvidenceOpenInput,
        "evidence_open",
        True,
    ),
    Operation(
        "evidence_revisions",
        "List the revisions of a record (oldest first), its current revision, aliases "
        "(former paths) and deletion state.",
        EvidenceRevisionsInput,
        "evidence_revisions",
        True,
    ),
    Operation(
        "cite_validate",
        "Validate a claim-to-evidence map: every cited span must resolve in scope, "
        "quotations must match their spans exactly, claims without resolvable evidence are "
        "reported. Not proof that the evidence entails the claims.",
        CitationRequest,
        "cite_validate",
        False,
    ),
    Operation(
        "cite_export",
        "Validate a claim-to-evidence map and export deterministic Markdown references "
        "and/or CSL-JSON, deduplicated by evidence identity (scope rechecked).",
        CitationExportRequest,
        "cite_export",
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
    "EvidenceOpenInput",
    "EvidenceOpenResult",
    "EvidenceRevisions",
    "EvidenceRevisionsInput",
    "KnowledgeService",
    "KnowledgeStatus",
    "MembershipInput",
    "MembershipResult",
    "NoInput",
    "Operation",
    "ProfileInput",
    "ProfileList",
    "ProjectContextLink",
    "ProjectContextLinkInput",
    "ProjectContextUnlinkInput",
    "ProjectCreate",
    "ProjectList",
    "RecordPage",
    "RecordQueryInput",
    "RecordRefIn",
    "RevisionEntry",
    "SourceList",
    "WorkspaceCreate",
    "WorkspaceList",
    "WorkspaceRef",
    "invoke",
]
