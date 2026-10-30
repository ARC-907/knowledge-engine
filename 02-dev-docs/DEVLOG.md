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
- **Evidence and citations (P3/P4).** `knowledge.db` schema 2 stores immutable
  revisions, aliases, spans, derivations and tombstones; `knowledge snapshot`
  records the corpus. Built in parallel with the broker on the pre-squash P0
  base and rebased once P2 merged. Integration choice: the broker's corpus
  open falls back to the evidence store only when the file exists with other
  content, so a deletion still reads `unavailable` (ADR 0012). Review found
  B-025: `migrate` and `snapshot` derived record ids separately; they now
  share one allocator.
- **Modules (P5/P6).** Built on the old P0 base with an ingestion interface
  and an in-memory double, then rebased onto the broker and merged with P3.
  The production adapter maps artifacts onto P3's ingest requests:
  - Sources are per workspace, because a source belongs to exactly one
    workspace: `module:<id>` in the default workspace and
    `module:<id>--<workspace>` elsewhere, which cannot collide because module
    ids never contain `--`.
  - P3's idempotency keys, `module:<artifact>:<run>`, make restarted runs replay
    and also serve as the durable artifact-to-revision index for lineage, so no
    new table was needed.
  - A record is refused rather than moved when a later run targets another
    project.
  - The CLI opens `knowledge.db` only when a call ingests, so module commands
    that never ingest create no database.
  - Windows CI found two defects in the module runtime (B-026, B-027). One of
    them was in a test helper: `os.kill(pid, 0)` is a harmless probe on POSIX
    but sends Ctrl+C on Windows, which ended the test run partway with a
    `KeyboardInterrupt`. Cancelling now stops the whole process tree on Windows
    too (`taskkill /T /F`), so the same test covers both platforms instead of
    being skipped on one.
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
- **P2 broker merged (#45).** CodeQL flagged a test `assert` wrapping a call
  with side effects; the call now runs before the assertion.
- **T1 tooling.** JSON log lines (`KE_LOG_FORMAT`, `KE_LOG_FILE`) feed a
  Loki/Alloy/Grafana stack under `deploy/observability`. Verified end to end
  here: the engine wrote JSON lines, Alloy shipped them with `level` and
  `component` labels, and every dashboard panel query returned data through
  Grafana's data-source proxy. Two dashboard bugs were found that way and fixed
  before commit: the HTTP-status panel parsed the raw JSON line instead of the
  message, and the component count summed over time steps. Findings:
  - uvicorn attaches an ANSI-coloured `color_message` to some records; the
    JSON formatter drops it.
  - Headless Chromium in this container reports the locale `en-US@posix`, which
    Grafana's bootstrap rejects; screenshots need an explicit browser locale.
  - `alloy fmt` uses tabs; the repository's `.editorconfig` now says so for
    `*.alloy`, and CI enforces the canonical form.
  - A CPU-only Ollama embeds `bge-m3` at about 1.6 s per chunk, so the
    engine's default batch of 32 hits the 60 s request timeout. The self-index
    script sends 8 per request; the embeddings guide now says to lower
    `KE_EMBED_BATCH` on such hosts. A full build of the repository's docs (43
    files) took about 9 minutes on four cores.
  - Serena 2.1.0 started on this repository with `.serena/project.yml`
    (tool interface forced) and answered `get_symbols_overview`, `find_symbol`
    and `find_referencing_symbols` for `EntryOwnership` in about 12 seconds.
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
