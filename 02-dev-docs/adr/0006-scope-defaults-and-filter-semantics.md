# ADR 0006 — Default legacy/workspace inclusion and scope filter semantics

- **Status:** Accepted (V2 P0/P1, issue #35)
- **Date:** 2026-10

## Context

V2 adds workspaces, projects, sources, collections, research types, tags and
curation status as orthogonal dimensions ([V2-DESIGN.md](../V2-DESIGN.md),
"Organization contract"). Existing corpus content has none of this metadata,
and the legacy `/search`, MCP `search` and CLI `search` surfaces must keep
their behaviour. A query that silently broadens to global retrieval, or a
typo that silently returns nothing, would both be scope mistakes.

## Decision

- **Legacy surfaces keep the legacy default.** `/search`, MCP `search` and CLI
  `search` do not read `knowledge.db`; they keep searching the enabled corpus
  entries exactly as before.
- **New surfaces require an explicit workspace.** `QueryContext.workspace_id`
  is mandatory. There is no implicit global workspace.
- **Legacy content is registered once, unclassified.** `knowledge migrate
  --apply` creates the configured default workspace
  (`KE_KNOWLEDGE_DEFAULT_WORKSPACE`, default `default`) and the source
  `corpus:base`, and registers every corpus file as research type `other`,
  curation status `unclassified`, in no project and no collection. It is not
  copied into projects.
- **Project queries exclude workspace-wide content** unless
  `include_workspace=true`; a workspace query (no `project_id`) covers every
  record in the workspace.
- **Filter semantics** (`knowledge_engine.knowledge.scope`):
  an omitted selection (`null`) takes that dimension from the named profile
  (`profile_id`, else `KE_KNOWLEDGE_PROFILE`; the built-in `default` profile
  selects every source of the workspace and nothing else); an explicit empty
  `source_ids` / `collection_ids` / `research_types` selects no sources and
  returns nothing; values within one list are ORed; `tags` are ANDed, and an
  explicit empty `tags` list means no tag constraint; dimensions are ANDed;
  unknown ids (workspace, project, source, collection, profile) are
  `unknown_scope` errors; a collection of another project inside a project
  query is `invalid_scope`. A scope error fails the whole request.
- **Adapters receive the resolved scope.** `SourceAdapter.search` and `open`
  take the caller's `QueryContext` (mode, limits, budgets) *and* the resolved
  `EffectiveScope`, because `null` in a `QueryContext` means "use the profile"
  and adapters must not resolve profiles or ids themselves.
- **Principals come from a trusted boundary.** `parse_query_context` rejects a
  `principal` in caller JSON; when trusted code attaches one, its
  `allowed_workspaces` are enforced (`denied`). Taxonomy is not access control.

## Consequences

- The store's SQL filter and the reference predicate `record_in_scope` are
  tested for parity on a fixture table; adapters added in P2 must pass the
  same table.
- Operators must run `knowledge migrate --apply` before the default workspace
  exists; admin and query calls before that fail with `unknown_scope`.
- A future change of any rule above is a contract change (new schema version).
