# Knowledge Engine Refactor Plan

Status: proposed
Date: 2026-06-07

## Goal

Bring the existing Knowledge Engine implementation into alignment with its
intended product model without rebuilding it. The work is a focused refactor:
name things clearly, organize docs and modules around stable concepts, expose
existing capabilities, and add missing unifying contracts where the current
system is already close.

## Current Read

The implementation already has the right ingredients:

- Base corpus registry and FTS search.
- Project-scoped documentation, logs, git context, pointers, scanner, and
  embedding tools.
- Board-scoped agent communication with MCP tools and physical scope DBs.
- Foundation DB for hosted tools, pipeline, provider bindings, chat/context
  artifacts, and future shared substrate.
- Hosted tool substrate for script/service/static capabilities.
- Sandbox adapter hook for an external execution engine such as
  OpenClaw/NemoClaw.

The main problem is organization and naming, not absence of architecture.

## Refactor Principles

1. Preserve working behavior.
2. Rename and reorganize through compatibility aliases where needed.
3. Treat empty registries as empty data, not missing features.
4. Keep stores sovereign; unify access through adapters and cards.
5. Make scope visible before expanding federation.
6. Document every renamed concept once in `docs/Reference/GLOSSARY.md`.

## Work Phases

### Phase 1: Stabilize Names And Docs

- Add a glossary/naming contract.
- Fix stale documentation links after moving docs into `Plans`, `Specs`, and
  `Reference`.
- Update `ARCHITECTURE.md`, `CATALOG.md`, and README language so the project
  reads as a capability/memory runtime, not just a markdown search app.
- Mark confusing legacy terms as aliases rather than deleting them.

### Phase 2: Inventory Existing Capability Surfaces

- Produce a machine-readable inventory of MCP tools, hosted tool tables,
  registry kinds, project-docs tools, board tools, and sandbox adapters.
- Add capability cards for existing sources without moving source ownership.
- Make "empty install" states explicit in the UI and APIs.

### Phase 3: Scope And Source Facade

- Add a lightweight source/scope API that reports active project, lane,
  board scope, corpus registry status, hosted tool count, and project-docs
  status.
- Use this to drive the dashboard scope bar.
- Do not introduce cross-scope search yet.

### Phase 4: Retrieval Broker Contract

- Define common request/result/open envelopes over the existing retrieval
  surfaces.
- Implement adapters for base corpus, project docs, board, hosted tools, and
  later global memory/catalog.
- Keep adapters source-owned; the broker routes and normalizes.

### Phase 5: Tool, Skill, And Kit Discoverability

- Surface skills, kits, hosted tools, and MCP tools as discoverable
  capabilities.
- Add policy fields for whether a capability can be used locally, by a lane,
  by another project, or globally.
- Add execution-adapter fields for sandbox/OpenClaw/NemoClaw handoff.

### Phase 6: Global Memory, Catalog, Federation

- Add global catalog as safe metadata first.
- Add global memory as an explicit promotion target.
- Add federation only after source selection, scope policy, and traces are in
  place.

## Immediate Next Tasks

1. Fix doc path references and add glossary.
2. Update architecture docs to describe the distributed retrieval layer.
3. Add a capability inventory command or script.
4. Add source/scope status endpoint.
5. Wire dashboard scope/source visibility.
