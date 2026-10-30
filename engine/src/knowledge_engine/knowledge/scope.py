"""Scope resolution and filter semantics (ADR 0006), free of any store or I/O.

:func:`resolve_scope` turns a caller's :class:`QueryContext` plus the
workspace's :class:`ScopeProfile` into the concrete
:class:`EffectiveScope` a request runs with, validating every id against a
:class:`ScopeCatalog` snapshot taken by the owning store.
:func:`record_in_scope` is the reference predicate: the metadata store's SQL
filter and every adapter's native filter must agree with it (parity tests in
``engine/tests/test_knowledge_scope.py``).

Semantics, as specified by the design:

=============================  ===============================================
Selection                      Meaning
=============================  ===============================================
dimension omitted (``None``)   use the profile's value for that dimension
``source_ids=[]`` (explicit)   no sources: the scope is empty, nothing matches
``collection_ids=[]``          no collections: nothing matches
``research_types=[]``          no types: nothing matches
values within one list         ORed (any of them)
``tags``                       ANDed (every tag must be present)
``tags=[]`` (explicit)         no tag constraint (overrides the profile's tags)
dimensions                     ANDed
unknown id                     ``unknown_scope`` error, never empty/global
``project_id`` set             only that project's records; plus records
                               without a project when ``include_workspace``
no ``project_id``              every record in the workspace
=============================  ===============================================
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .contracts import (
    EffectiveScope,
    QueryContext,
    ResearchType,
    ScopeProfile,
)
from .errors import DeniedError, InvalidScopeError, UnknownScopeError

#: The profile used when a workspace has none stored under the configured name.
DEFAULT_PROFILE_ID = "default"


@dataclass(frozen=True)
class ScopeCatalog:
    """A consistent snapshot of one workspace's scope metadata.

    ``collections`` maps a collection id to the project it belongs to
    (``None`` for a workspace-wide collection).
    """

    workspace_id: str
    projects: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()
    collections: Mapping[str, str | None] = field(default_factory=dict)
    profiles: Mapping[str, ScopeProfile] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordView:
    """The scope-relevant metadata of one record."""

    source_id: str
    record_id: str
    project_id: str | None
    research_type: ResearchType
    tags: frozenset[str] = frozenset()
    collection_ids: frozenset[str] = frozenset()


def builtin_profile(workspace_id: str, profile_id: str = DEFAULT_PROFILE_ID) -> ScopeProfile:
    """The built-in profile: every source of the workspace, no other constraint."""
    return ScopeProfile(workspace_id=workspace_id, profile_id=profile_id)


def _profile(context: QueryContext, catalog: ScopeCatalog, configured: str) -> ScopeProfile:
    name = context.profile_id or configured
    stored = catalog.profiles.get(name)
    if stored is not None:
        return stored
    if name == DEFAULT_PROFILE_ID:
        return builtin_profile(catalog.workspace_id)
    raise UnknownScopeError(
        f"unknown profile {name!r} in workspace {catalog.workspace_id!r}",
        details={"kind": "profile", "id": name, "workspace_id": catalog.workspace_id},
    )


def _unknown(kind: str, ids: list[str], workspace_id: str) -> UnknownScopeError:
    return UnknownScopeError(
        f"unknown {kind} id(s) in workspace {workspace_id!r}: {', '.join(ids)}",
        details={"kind": kind, "ids": ids, "workspace_id": workspace_id},
    )


def resolve_scope(
    context: QueryContext,
    catalog: ScopeCatalog,
    *,
    configured_profile: str = DEFAULT_PROFILE_ID,
) -> EffectiveScope:
    """Resolve ``context`` against ``catalog``; raise a typed error on any bad id.

    ``catalog`` must describe ``context.workspace_id`` (an unknown workspace
    is reported by the store before a catalog exists).
    """
    if context.workspace_id != catalog.workspace_id:
        raise InvalidScopeError(
            "the scope catalog belongs to another workspace",
            details={"workspace_id": context.workspace_id},
        )
    principal = context.principal
    if principal is not None and context.workspace_id not in principal.allowed_workspaces:
        raise DeniedError(
            f"principal may not query workspace {context.workspace_id!r}",
            details={"workspace_id": context.workspace_id},
        )
    if context.project_id is not None and context.project_id not in catalog.projects:
        raise _unknown("project", [context.project_id], catalog.workspace_id)
    profile = _profile(context, catalog, configured_profile)
    if profile.workspace_id != catalog.workspace_id:
        raise InvalidScopeError(
            "profile belongs to another workspace", details={"profile_id": profile.profile_id}
        )

    from_profile: list[str] = []

    def pick[T](name: str, explicit: T | None, fallback: T) -> T:
        if explicit is None:
            from_profile.append(name)
            return fallback
        return explicit

    sources = pick("source_ids", context.source_ids, profile.source_ids)
    collections = pick("collection_ids", context.collection_ids, profile.collection_ids)
    types = pick("research_types", context.research_types, profile.research_types)
    tags = pick("tags", context.tags, profile.tags)

    if sources is not None:
        missing = sorted(set(sources) - catalog.sources)
        if missing:
            raise _unknown("source", missing, catalog.workspace_id)
        resolved_sources = tuple(sorted(set(sources)))
    else:
        resolved_sources = tuple(sorted(catalog.sources))

    if collections is not None:
        missing = sorted(set(collections) - set(catalog.collections))
        if missing:
            raise _unknown("collection", missing, catalog.workspace_id)
        if context.project_id is not None:
            foreign = sorted(
                cid
                for cid in collections
                if catalog.collections[cid] not in (None, context.project_id)
            )
            if foreign:
                raise InvalidScopeError(
                    f"collection(s) of another project: {', '.join(foreign)}",
                    details={"collection_ids": foreign, "project_id": context.project_id},
                )
        collections = tuple(sorted(set(collections)))

    if types is not None:
        types = tuple(sorted(set(types), key=lambda t: t.value))

    empty = (
        (sources is not None and not sources)
        or (collections is not None and not collections)
        or (types is not None and not types)
    )
    return EffectiveScope(
        workspace_id=catalog.workspace_id,
        project_id=context.project_id,
        include_workspace=context.include_workspace if context.project_id else False,
        profile_id=profile.profile_id,
        source_ids=() if empty else resolved_sources,
        collection_ids=collections,
        research_types=types,
        tags=tuple(sorted(set(tags))),
        from_profile=tuple(from_profile),
        empty=empty,
    )


def record_in_scope(record: RecordView, scope: EffectiveScope) -> bool:
    """The reference filter: does ``record`` belong to ``scope``?"""
    if scope.empty or record.source_id not in scope.source_ids:
        return False
    if (
        scope.project_id is not None
        and record.project_id != scope.project_id
        and not (scope.include_workspace and record.project_id is None)
    ):
        return False
    if scope.collection_ids is not None and not (record.collection_ids & set(scope.collection_ids)):
        return False
    if scope.research_types is not None and record.research_type not in scope.research_types:
        return False
    return set(scope.tags) <= record.tags


__all__ = [
    "DEFAULT_PROFILE_ID",
    "EffectiveScope",
    "RecordView",
    "ScopeCatalog",
    "builtin_profile",
    "record_in_scope",
    "resolve_scope",
]
