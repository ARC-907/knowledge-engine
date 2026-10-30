# Development log

Dated notes on what was done, why, and what was learned — the context that
does not fit in a commit message or a CHANGELOG line. Newest first. Decisions
with lasting effect also get an [ADR](adr/README.md); defects get a row in the
[bug log](BUGLOG.md).

## 2026-10-30 — V2 implementation begins

- **Plan.** The V2 design ([V2-DESIGN.md](V2-DESIGN.md)) is implemented as a
  train of slices in the order recorded in [ROADMAP.md](ROADMAP.md): R0 and the
  browser CI job first, then the contracts and metadata store, then the broker,
  evidence/citations and modules (built in parallel on stacked branches, landed
  one at a time), then tooling, Serena, the dashboard and qualification.
- **R0 (#39).** Reproduced before fixing: FTS and hybrid returned a nested file
  twice. Decided *ownership* (deepest enabled entry) rather than *membership*;
  many-to-many grouping belongs to V2 collections ([ADR 0005](adr/0005-nested-entry-ownership.md)).
  Windows CI then caught a separator bug in the fix (B-024).
- **Browser CI (#40).** Turning the job on found that both browser tests had
  been broken since 3.0 (B-022). The job now fails when the tests skip, so a
  missing browser cannot pass silently.
- **Contracts and metadata (P0/P1).** `knowledge_engine.knowledge` defines the
  versioned contracts; `knowledge.db` holds workspaces, projects, collections
  and classification; `knowledge migrate` backfills the corpus with a backup
  and checkpoints. Adapters receive the resolved scope (ADR 0006).
- **Serena compatibility probe.** Installed `serena-agent==2.1.0` in an
  isolated environment and drove it over stdio with the MCP SDK client
  (protocol `2026-07-28`). Findings that shape the adapter:
  - The default `agent` context uses Serena's REPL interface, which exposes
    only a code-execution tool; the adapter must force `--agent-interface tools`.
  - Serena reports usage to its vendor at startup unless
    `SERENA_USAGE_REPORTING=false`.
  - The Python language server (pyright 1.1.403) is launched through `uvx`,
    so `uv` must be on Serena's `PATH`.
  - One process per project kept two projects with identical symbol names
    isolated; `find_symbol`, `get_symbols_overview` and
    `find_referencing_symbols` return JSON text with 0-based inclusive lines.
  - Package license metadata: GPL-3.0-or-later. Serena stays an operator
    install and is never bundled.
- **Local infrastructure.** Docker works in the development container; Docker
  Hub rate-limits anonymous pulls, so images come through the
  `mirror.gcr.io` mirror. Containers that need the outbound proxy run with host
  networking. Ollama runs locally with `bge-m3` for embedding work.

## 2026-10-24 — 3.0.0 prepared

- Release commit sets the version to 3.0.0 (#32); review follow-ups landed in
  #33 (lifecycle-aware watcher, MCP limit bound, board health shape). The tag
  and GitHub Release are held for the maintainer.
- The roadmap document records the 3.0 train and remaining maintainer actions.

## 2026-10-22 → 2026-10-24 — the 3.0 modernization train

- Audit of the 2.0 tree: path regressions, version drift, open board admin
  routes, duplicated clients, no tooling configuration, stale docs (see the
  [bug log](BUGLOG.md), 3.0 section).
- Delivered as fourteen stacked PRs with a local gate (ruff, mypy, pytest on
  3.12/3.13/3.14, build, clean-venv wheel smoke) plus a multi-lens review pass
  before each merge ([ADR 0004](adr/0004-merge-train.md)).
- Lessons:
  - Windows and macOS CI jobs found real bugs (B-014, B-015, B-017) even as
    advisory checks; keep running them on every PR.
  - CodeQL notes (import cycles, unused globals, broad `except`) are cheap to
    fix immediately and are now part of the review brief for every slice.
  - Gates that share virtual environments must be serialized (a lock file in
    the gate script) or they produce spurious import errors.
