# ADR 0009 — Module catalog persistence and restart behaviour

- **Status:** Accepted (V2 P0, issue #37; implemented by P5)
- **Date:** 2026-10

## Context

Modules (script, service or external MCP utilities) become MCP tools through
one catalog and dispatcher ([V2-DESIGN.md](../V2-DESIGN.md), "Modular
utility contract"). MCP clients cache the tool list of a session; the SDK
and common clients handle `tools/list_changed` unevenly.

## Decision

- The catalog persists **intent and configuration** (installed manifest,
  enabled flag, operator configuration with secret references) separately
  from **observed health** (lifecycle state, last error), which is runtime
  state and is not trusted across restarts.
- In v1 the advertised MCP tool list is **frozen per server process**:
  enabling, disabling, installing or reconfiguring a module takes effect on
  the next restart. Disable still stops new work immediately (calls fail with
  `unavailable`) without changing the advertised schema.
- Public tool names derive deterministically from module id and tool id;
  collisions and built-in names (`search`, `registry_*`, `project_context.*`,
  `board_*`, `knowledge_*`) are rejected at enable time.
- Failures use the shared codes in `knowledge_engine.knowledge.errors`.

## Consequences

- Hot reload and change notifications are later work with their own ADR.
- The `knowledge_*` admin tools of this slice follow the same rule: they are
  part of the frozen built-in surface and toggled by `KE_KNOWLEDGE_ENABLED`
  at startup.
