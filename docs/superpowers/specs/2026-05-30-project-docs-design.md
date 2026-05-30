# Project-Specific Documentation Library — Design Spec

**Status:** proposed
**Date:** 2026-05-30
**Target:** `deliverables/v1.0-template/engine/src/knowledge_engine/project_docs/`
**Scope decision:** build all phases; Phase 1 (identity + store + read) is the first vertical slice.

This document resolves the ambiguities in the feature brief into concrete decisions,
interfaces, schemas, and tool contracts so the implementation can be parallelized across
subagents against a stable foundation. The feature brief is the requirements source of
truth; *this* document is the architecture source of truth. Where the two conflict, this
document's concrete choices win, but never at the expense of the brief's safety defaults.

---

## 1. Guiding decisions (resolutions of brief ambiguity)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Project-docs is free engine code**, not paid-bundle content. All fixtures sanitized/generic. | The paid Polar bundle is *content*; the engine is MIT. This is infrastructure. |
| D2 | **TOML config is a new, separate layer** (`knowledge-engine.toml`), discovered by walking up from CWD or `KE_CONFIG_PATH`. The existing `Config.from_env()` is untouched and still governs the base engine. | Brief mandates TOML; the base engine is env-driven. Keep them decoupled; project-docs owns its own config object. |
| D3 | **Per-project SQLite DB** for documentation content; **one shared registry DB** for identity (fingerprints). Branch is a *column*, not a separate file. | Content isolation = privacy + portability + clean deletion. Identity centralized for collision detection + cross-project listing. |
| D4 | **Semi-deterministic fingerprints**: derived by hashing canonical inputs, then *recorded* in the registry (with collision detection + manual override). | Same project/branch → same fingerprint across scans without external state, but the registry is authoritative and overridable. |
| D5 | **MCP server refactored to a pluggable tool-module registry.** Existing 4 tools preserved verbatim. Project-docs tools live in `project_docs/mcp_tools/` and are merged in. | ~50 new tools cannot live in one hand-edited list; but we must not break the existing surface. |
| D6 | **Migration runner** (ordered `.sql` files + `schema_version` table) for project-docs DBs. Base engine's inline-schema style is left as-is. | 20+ tables across 2 DB shapes warrant versioned migrations; inline `executescript` does not scale here. |
| D7 | **All git access via `subprocess` argv lists**, never `shell=True`. Git fully optional; absence degrades gracefully. | Matches the repo's recent `host.py` security fix. |
| D8 | **Compact-by-default everywhere.** Tools return summaries + pointers; full content/raw logs/embedding-search are config-gated *and* per-call explicit. | Token-cost reduction is a primary goal; safety defaults are conservative. |
| D9 | **Single pointer URI grammar** with a type segment; `KE-DOCSTRING` is the docstring profile. | Brief's example scheme generalized so logs/records share resolution. |
| D10 | **Provider abstraction via small ABCs** (`EmbeddingProvider`, `SummarizerProvider`, `ScannerBackend`). Default impls are local/no-op. No remote calls unless explicitly configured. | Local-first; off-by-default; no vendor lock-in. |

---

## 2. Package layout

Under `deliverables/v1.0-template/engine/src/knowledge_engine/project_docs/`:

```
project_docs/
  __init__.py            # public API surface (re-exports), feature flag check
  config.py              # TOML loader -> dataclasses; conservative defaults
  paths.py               # resolve project root, .knowledge-engine dirs, db paths
  db.py                  # connection factory (WAL, FK on), migration runner
  fingerprints.py        # project/branch fingerprint allocation + validation + collision
  registry.py            # project registration / listing / validation (registry DB)
  models.py              # dataclasses for records (DocRecord, TestRun, Pointer, ...)
  schema.py              # constants: categories, subtypes, statuses, sanitization states
  sanitize.py            # sanitization strategy (secrets, PII, paths, oversize, binary)
  hashing.py             # content + sanitized-content hashing helpers
  ingest.py              # ingestion pipeline orchestration (the 18 stages)
  search.py              # FTS5 query + filter builder (project/branch/type/path/commit...)
  pointers.py            # pointer URI grammar, allocation, resolution, backrefs
  git_context.py         # optional git metadata / diff-summary collection (argv subprocess)
  logs.py                # test/build/runtime/diagnostic log modeling + classification
  scanner/
    __init__.py
    base.py              # Detector ABC, Candidate dataclass, ScanResult
    discovery.py         # orchestrates detectors, respects gitignore/size/symlink rules
    markdown.py          # markdown / long-form / versioned doc / devlog / Q&A / design-note
    docstrings.py        # source-language docstring extraction (py first; pluggable)
    comments.py          # structured-comment detection (off by default)
    logs.py              # test/build/runtime log discovery
    report.py            # Mode 1: report-only (no writes)
    ingest.py            # Mode 2: ingest (validates fingerprints, runs pipeline)
    pointer_plan.py      # Mode 3: pointer-replacement plan (no writes)
    pointer_apply.py     # Mode 4: guarded source mutation (backups, audit, rollback)
    validators.py        # pre-flight validation (fingerprints, config gates, paths)
  embeddings/
    __init__.py
    providers.py         # EmbeddingProvider ABC + registry
    local.py             # Ollama provider (reuses bge-m3 patterns)
    remote.py            # remote HTTP provider stub (off unless allow_remote_provider)
    index.py             # vector store in project DB; FTS-independent semantic search
  mcp_tools/
    __init__.py          # collect_tools() -> (tool_defs, dispatch_map)
    base.py              # ToolModule protocol, result helpers (summary/full envelopes)
    registry_tools.py
    query_tools.py
    pointer_tools.py
    scanner_tools.py
    log_tools.py
    git_tools.py
    embedding_tools.py
    capability_tools.py
  migrations/
    001_project_fingerprints.sql   # registry DB
    002_project_docs.sql           # project DB core records
    003_project_docs_fts.sql       # project DB FTS5
    004_logs_tests.sql             # project DB logs/tests
    005_pointers.sql               # project DB pointers
    006_git_context.sql            # project DB git
    007_embeddings.sql             # project DB embedding metadata
  cli.py                 # `knowledge-engine project-docs ...` subcommands
  docs/
    PROJECT_DOCS.md
    PROJECT_DOCS_CONFIG.md
    PROJECT_DOCS_SCANNER.md
    PROJECT_DOCS_MCP_TOOLS.md
    PROJECT_DOCS_POINTERS.md
```

Tests under `engine/tests/project_docs/` with a sanitized fixture project under
`engine/tests/fixtures/sample_project/`.

---

## 3. Configuration

New file `knowledge-engine.toml` (root of the user's *own* project, not the engine repo).
Discovery order: `KE_CONFIG_PATH` env → walk up from CWD looking for `knowledge-engine.toml`
→ if none found, **all defaults apply and `project_docs.enabled = false`** (feature is dark).

`config.py` parses with `tomllib` (3.11+) / `tomli` (3.10 backport, added as a conditional
dep) into frozen dataclasses mirroring the brief's schema verbatim. Every field has a safe
default baked into the dataclass, so a partial or absent file is valid.

Permission gates (all default-deny except read): `scanner.enabled`, `scanner.pointer_replacement.enabled`,
`scanner.pointer_replacement.allow_source_mutation`, `ingestion.retain_raw_content`,
`git.include_full_diffs`, `embeddings.enabled`, `embeddings.allow_remote_provider`,
`mcp.allow_full_content`, `mcp.allow_raw_logs`, `mcp.allow_embedding_search`, `mcp.allow_mutating_tools`.

The full default TOML is the brief's example, adopted as-is. `config.py` additionally exposes
`PROJECT_DOCS_DEFAULTS` so tests and docs render the canonical defaults without duplication.

---

## 4. Identity & fingerprints

**Project fingerprint** — `proj_<b32lower(sha256(canonical_root [+ remote_identity]))[:16]>`.
- `canonical_root`: the absolute, normalized project root (case-folded on Windows). Never stored
  raw in content DBs; the registry stores it (local, private file) for matching.
- `remote_identity` (optional): sanitized git remote (host+path, no creds), hashed.
- Recorded in `project_fingerprints`; manual override via config or `register_project(fingerprint=...)`.

**Branch fingerprint** — `br_<b32lower(sha256(project_fingerprint + ":" + branch_name))[:16]>`.
- Allocated on demand; retroactively allocated if a record references an unknown branch.

**Validation before every write**: `validators.require_context(project_fp, branch_fp)` confirms both
exist in the registry and match the active detected/configured context, or raises `ContextError`.
`fingerprint_events` logs every allocation/override/collision for auditability.

Collision detection: if a derived fingerprint maps to a *different* recorded root, raise and require
explicit override — never silently co-mingle two projects.

---

## 5. SQLite schema

### 5.1 Registry DB (`project-fingerprints.sqlite`, shared)
- `schema_version(version INTEGER, applied_at TEXT)`
- `projects(project_fp PK, name, root_path, remote_identity_hash, created_at, updated_at, notes)`
- `branches(branch_fp PK, project_fp FK, branch_name, created_at, updated_at, UNIQUE(project_fp,branch_name))`
- `project_fingerprints(project_fp PK, strategy, source_inputs_hash, manual_override INT, created_at)`
- `branch_fingerprints(branch_fp PK, project_fp FK, strategy, manual_override INT, created_at)`
- `fingerprint_events(id PK, ts, kind, project_fp, branch_fp, detail, data_json)`

### 5.2 Project DB (`<project_slug>.sqlite`, one per project)
Core records:
- `project_docs(record_id PK, pointer_id, project_fp, branch_fp, project_name, branch_name,
  source_path, source_uri, category, subtype, content_hash, sanitized_content_hash,
  raw_retained INT, sanitization_status, ingestion_status, created_at, updated_at,
  source_modified_at, git_commit, git_branch, git_dirty_json, summary, ingestion_run_id)`
- `project_doc_bodies(record_id FK, searchable_body, raw_body NULLABLE)` — raw_body only when configured
- `project_doc_summaries(record_id FK, summary, summarizer, created_at)`
- `project_doc_links(id PK, src_record_id, dst_record_id, link_type)` — parent/child/related/code-span
- `project_doc_provenance(record_id FK, ingestion_run_id, detector, source_path, source_span_json, notes)`
- `project_doc_ingestion_runs(ingestion_run_id PK, project_fp, branch_fp, mode, started_at, finished_at, stats_json, status)`

Search:
- `project_docs_fts` — FTS5(searchable_body, summary, content='', tokenize='porter unicode61');
  `rowid` = `project_docs.rowid`; populated explicitly during ingest, deletable by rowid.

Logs/tests:
- `test_runs(id PK, project_fp, branch_fp, command, framework, target, exit_code, classification,
  started_at, duration_ms, git_commit, git_dirty_json, summary, failure_summary, raw_retained INT)`
- `test_log_records(id PK, test_run_id FK, record_id NULLABLE, sanitized_log, raw_log NULLABLE)`
- `build_log_records(id PK, project_fp, branch_fp, command, exit_code, classification, started_at,
  duration_ms, git_commit, summary, sanitized_log, raw_log NULLABLE)`
- `runtime_log_records(...)` — same shape; off by default

Pointers:
- `doc_pointers(pointer_id PK, record_id FK, scheme, project_fp, branch_fp, source_path,
  source_span_json, content_hash, created_at, status)`
- `pointer_backrefs(id PK, pointer_id FK, ref_source_path, ref_span_json, created_at)`
- `pointer_rewrite_plans(plan_id PK, created_at, dry_run INT, items_json, status)`
- `pointer_rewrite_events(id PK, plan_id FK, pointer_id, ts, action, backup_path, result, detail)`

Git:
- `git_context(id PK, project_fp, branch_fp, captured_at, branch, commit, dirty INT, remote_hash, data_json)`
- `git_commits(commit PK, project_fp, author_hash, committed_at, subject, body_summary)`
- `git_diff_summaries(id PK, project_fp, from_ref, to_ref, files_changed, insertions, deletions, summary)`
- `git_diffs(id PK, ...)` — created only when `git.include_full_diffs = true`

Embeddings:
- `doc_embeddings(record_id FK, provider, model, dim, vector BLOB, created_at)` — struct-packed floats

Every project DB carries its own `schema_version`.

---

## 6. Pointer grammar

```
ke-doc://<type>/project/<project_fp>/branch/<branch_fp>/<kind>/<record_id>
KE-DOCSTRING://project/<project_fp>/branch/<branch_fp>/doc/<record_id>   # docstring profile alias
```
- `type` ∈ {docstring, doc, testlog, buildlog, note}. The `KE-DOCSTRING://` form is a recognized
  alias that maps to `type=docstring`.
- **Resolution** (`pointers.resolve`) returns: stored content (summary by default; full only if
  permitted), source_path, original span, project_fp, branch_fp, git_commit (if any),
  ingestion_run, sanitization_status, related records.
- **Source insertion form** (when applying a replacement): the docstring is replaced by a minimal,
  language-appropriate stub containing the URI + a one-line human hint + the content hash, e.g.
  Python: `"""See ke-doc://docstring/project/<fp>/branch/<fp>/doc/<id> (sha:<8>)."""`.
- Reversible: original span + a backup file are stored; `pointer_apply` records a rollback path.

---

## 7. Ingestion pipeline (`ingest.py`)

Explicit, individually testable stages: load config → resolve project root → resolve/allocate
project fp → resolve/allocate branch fp → validate context → acquire source (scanner or direct)
→ classify → include/exclude filter → size-limit → sanitize → hash (raw+sanitized) → dedupe by
hash+source → summarize (if configured) → store normalized record → update FTS5 → embed (if enabled)
→ emit run report. Each stage is a pure-ish function taking/returning typed records; the orchestrator
wires them. Dedupe is by `(content_hash, source_path, branch_fp)`.

---

## 8. Scanner

`Detector` ABC: `discover(root, cfg) -> Iterable[Candidate]`. Detectors are registered and selected
by config discovery flags. `discovery.py` enforces `respect_gitignore`, `follow_symlinks=false`,
`max_file_bytes`. Modes:

1. **report** — runs detectors, returns candidates + types + est. size + sanitization-risk flags +
   git-availability + pointer candidates + recommended next actions. **No writes.**
2. **ingest** — validates fingerprints, runs detectors → ingestion pipeline. Writes records.
3. **pointer-plan** — produces rewrite plan (target file/span, content hash, proposed pointer,
   replacement preview, backup plan, risk flags, reversibility). **No writes.**
4. **pointer-apply** — guarded mutation: requires `enabled` + `allow_source_mutation` + explicit
   non-dry-run + a plan + backup + pointer-resolution validation; emits audit events; rollback path.
5. **embedding-enrichment** — generates/refreshes embeddings only when embeddings enabled.

---

## 9. MCP surface

`mcp_server.py` refactor: build `TOOLS` and the dispatch map from a base set (existing 4 tools,
unchanged) plus `project_docs.mcp_tools.collect_tools()` (gated on `project_docs.mcp.enabled`).
Tool names use the brief's `project_docs.<group>_<verb>` namespace. Result envelopes default to
`summary` mode; `mode="full"` requires the relevant `allow_*` gate or returns a structured
`{"status":"not_permitted", ...}`. Embedding/mutating/raw-log tools return
`{"status":"disabled"|"not_configured"}` when their gate is off — never an error, so agents can
discover capability. `capability_tools` (`capabilities`, `config_status`, `healthcheck`,
`explain_available_tools`) let an agent introspect before attempting heavier workflows.

All tool groups from the brief are implemented: registry, query, pointer, scanner, log/test,
git/lineage, embedding, capability.

---

## 10. Sanitization

`sanitize.py` returns `(text, status, redactions)` where status ∈ {sanitized, raw_omitted,
raw_retained, redacted, rejected_oversize, rejected_binary, rejected_unsafe}. Rules: secret/API-key/
token patterns, env-var values, credentials in URLs, private absolute paths → placeholder, optional
email/PII redaction, binary/oversize rejection, stack-trace secret scrubbing. Rule set is a list of
`Rule(name, pattern, action)` so it is extensible and testable. Public fixtures only ever exercise
synthetic secrets.

---

## 11. Testing strategy

pytest, offline. Per-module unit tests; fingerprint determinism + collision tests; sanitization
table tests (synthetic secrets); ingestion stage tests; FTS round-trip + filter tests; pointer
grammar parse/resolve + rewrite dry-run/apply/rollback tests; scanner report-mode golden test on the
fixture project; config default tests; MCP tool contract tests (gates return disabled/not-permitted
correctly). Git + embeddings tests are skipped/mocked when the binary/provider is absent. CI (`ruff
+ pytest`) must stay green.

---

## 12. Phasing (build order; all phases in scope)

- **P0 Foundation:** config, paths, db + migration runner, models, schema constants, package skeleton.
- **P1 Identity + store + read (first slice):** fingerprints, registry, sanitize, hashing, ingest
  (direct), search, capability + registry + query MCP tools, CLI subset, migrations 001–003.
- **P2 Scanner:** detectors, discovery, report + ingest modes, scanner tools, migration 004 wiring.
- **P3 Logs/tests + Git:** logs.py, test/build/runtime records, git_context, log + git tools,
  migrations 004 (logs) + 006.
- **P4 Pointers:** pointers.py, pointer_plan/apply, pointer tools, migration 005, mutation safety.
- **P5 Embeddings:** providers/local/remote/index, embedding tools, migration 007.
- **P6 Docs + README:** product docs under `project_docs/docs/`, README section, CHANGELOG/CATALOG.

P0 is built first and sequentially (it defines every interface). P1–P5 leaf modules are then
parallelizable across subagents because each owns distinct files against the P0 interfaces;
integration (MCP wiring, CLI, cross-module tests) is reconciled by the main thread.

---

## 13. Risks, tradeoffs, non-goals

**Risks:** (a) parallel agents touching shared files — mitigated by P0-first + file ownership
partition; (b) source mutation — mitigated by multi-gate + backups + dry-run default + audit;
(c) sanitization gaps — mitigated by default-deny raw retention + conservative rejection; (d) FTS
sync drift — mitigated by explicit upsert/delete keyed on rowid during ingest.

**Tradeoffs:** per-project DBs add file management vs. a single DB, but win on isolation/privacy.
Semi-deterministic fingerprints trade perfect determinism for collision-safety via the registry.

**Non-goals (from brief):** no private content in the repo; scanner/embeddings/git/diffs/pointer-apply
all off by default; no required provider; no hidden telemetry; no monolith; the base engine's
existing config/tools remain untouched and working.

---

## 14. Success criteria

Mirrors the brief's success list: register a project; allocate+validate fingerprints; scanner report
mode; sanitized ingest; normalized SQLite records; FTS5 without embeddings; optional embeddings;
first-class test/build logs; optional git metadata; diff summaries without full diffs; opt-in
reversible pointer replacement; pointer resolution via MCP; all beneficial systems exposed as tools;
compact-by-default responses; full content only on explicit permitted request; public-repo hygiene
preserved; modular, provider-abstracted, local-first, configurable; extensible without core rewrite.
