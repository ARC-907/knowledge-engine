# ADR 0010 — Exact Serena compatibility mapping deferred to the Serena slice

- **Status:** Superseded by [ADR 0016](0016-serena-read-only-adapter.md) (was: accepted deferral; V2 P0, issue #38)
- **Date:** 2026-10

## Context

The design asks for an exact Serena tool mapping before release
([V2-DESIGN.md](../V2-DESIGN.md), "Serena integration"). Serena's tool
schemas depend on the pinned upstream version, which is chosen when the
adapter is implemented (roadmap step P7).

## Decision

- This contract slice fixes only what Serena results must map **onto**: the
  `SourceAdapter` protocol, `code` locators (commit or snapshot digest,
  1-based exclusive-end lines, ADR 0007) and the typed error codes
  (`unavailable`, `incompatible`, ...).
- The exact mapping (pinned version/commit, protocol revision, allowlisted
  read tools, argument/result translation, language backends) is recorded by
  the Serena slice in its own ADR, superseding this one. Until then no Serena
  tool is exposed, and an absent or unmapped Serena is `incompatible` or
  `unavailable`, never a core failure.

## Consequences

- Nothing in the core imports or depends on Serena.
- The license review of the pinned release is part of the Serena slice.
