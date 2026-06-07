# Project-Specific Documentation Library — Implementation Plan

> Implementation plan that drove the `project_docs` subsystem. Steps use
> checkbox (`- [ ]`) syntax so the work can be tracked task-by-task by either
> a human or a subagent driver. Tasks are written to be picked up
> independently against the frozen P0 interfaces.

**Goal:** Add a `project_docs` subsystem to the Knowledge-Engine that ingests sanitized, fingerprinted, branch-aware project documentation/logs/git-context into per-project SQLite stores, exposes them as compact-by-default MCP tools, and offers an opt-in scanner with reversible docstring-pointer rewriting and optional embeddings.

**Architecture:** New package `knowledge_engine.project_docs` with a TOML config layer, a shared fingerprint registry DB, per-project content DBs (versioned migrations), an ingestion pipeline, FTS5 search, an optional integrated scanner (5 modes), provider-abstracted embeddings, and a pluggable MCP tool registry merged into the existing hand-rolled server. Conservative defaults: scanner/pointers/embeddings/git-diffs/raw-logs all OFF.

**Tech Stack:** Python 3.10+, stdlib `sqlite3` (FTS5), `tomllib`/`tomli`, `subprocess` (argv) for git, optional Ollama for embeddings, pytest, ruff.

**Reference spec:** `docs/Specs/2026-05-30-project-docs-design.md` (architecture source of truth). Read it before any task.

**Base directory for all paths below:** `deliverables/v1.0-template/engine/`

---

## File Structure & Ownership

Each file has one responsibility; parallel agents own disjoint files. Interfaces are defined in P0 (Tasks 1–7) and are FROZEN once P0 is committed — later tasks import, never redefine.

| File | Responsibility | Phase |
|---|---|---|
| `src/knowledge_engine/project_docs/__init__.py` | Public re-exports, `is_enabled()` | P0 |
| `.../project_docs/config.py` | TOML → frozen dataclasses, defaults, gates | P0 |
| `.../project_docs/paths.py` | Resolve project root, `.knowledge-engine/` dirs, DB paths, slug | P0 |
| `.../project_docs/db.py` | Connection factory (WAL/FK), migration runner, `schema_version` | P0 |
| `.../project_docs/models.py` | Dataclasses: `DocRecord`, `TestRun`, `LogRecord`, `Pointer`, `Candidate`, `GitContext`, `IngestionRun`, `ScanReport` | P0 |
| `.../project_docs/schema.py` | Enums/const: categories, subtypes, statuses, sanitization states, link types | P0 |
| `.../project_docs/hashing.py` | `content_hash`, `sanitized_hash`, `short_hash` | P0 |
| `.../project_docs/migrations/00{1..7}_*.sql` | DDL | P0 (files) |
| `.../project_docs/sanitize.py` | `sanitize(text, cfg) -> SanitizeResult` | P1 |
| `.../project_docs/fingerprints.py` | allocate/resolve/validate project & branch fps, collisions | P1 |
| `.../project_docs/registry.py` | register/list/validate projects; current context | P1 |
| `.../project_docs/ingest.py` | pipeline orchestration (direct ingest) | P1 |
| `.../project_docs/search.py` | FTS5 query + filter builder | P1 |
| `.../project_docs/scanner/base.py` | `Detector` ABC, `Candidate`, `ScanResult` | P2 |
| `.../project_docs/scanner/discovery.py` | walk + gitignore/size/symlink rules, detector dispatch | P2 |
| `.../project_docs/scanner/markdown.py` | markdown/devlog/qa/design-note/versioned detectors | P2 |
| `.../project_docs/scanner/docstrings.py` | python docstring extraction (pluggable per-language) | P2 |
| `.../project_docs/scanner/comments.py` | structured-comment detector (off by default) | P2 |
| `.../project_docs/scanner/logs.py` | test/build/runtime log discovery | P2/P3 |
| `.../project_docs/scanner/report.py` | Mode 1 report-only | P2 |
| `.../project_docs/scanner/ingest.py` | Mode 2 ingest (uses pipeline) | P2 |
| `.../project_docs/scanner/pointer_plan.py` | Mode 3 plan | P4 |
| `.../project_docs/scanner/pointer_apply.py` | Mode 4 guarded apply | P4 |
| `.../project_docs/scanner/validators.py` | pre-flight gates | P2 |
| `.../project_docs/logs.py` | test/build/runtime record store + classification | P3 |
| `.../project_docs/git_context.py` | optional git via argv subprocess | P3 |
| `.../project_docs/pointers.py` | URI grammar, allocate, resolve, backrefs | P4 |
| `.../project_docs/embeddings/providers.py` | `EmbeddingProvider` ABC + factory | P5 |
| `.../project_docs/embeddings/local.py` | Ollama provider | P5 |
| `.../project_docs/embeddings/remote.py` | remote HTTP stub (gated) | P5 |
| `.../project_docs/embeddings/index.py` | vector store + semantic search | P5 |
| `.../project_docs/mcp_tools/base.py` | `ToolModule`, envelope helpers | P1 |
| `.../project_docs/mcp_tools/{registry,query,pointer,scanner,log,git,embedding,capability}_tools.py` | tool defs + handlers | per phase |
| `.../project_docs/mcp_tools/__init__.py` | `collect_tools(cfg)` | P1 |
| `.../project_docs/cli.py` | `project-docs` subcommands | per phase |
| `.../project_docs/docs/*.md` | product docs | P6 |
| `src/knowledge_engine/mcp_server.py` | MODIFY: merge project_docs tools | P1 |
| `src/knowledge_engine/cli.py` | MODIFY: add `project-docs` group | P1 |
| `pyproject.toml` | MODIFY: add `tomli` (py<3.11), optional `project-docs` extras | P0 |
| `tests/project_docs/*` , `tests/fixtures/sample_project/*` | tests + sanitized fixture | all |

---

## P0 — Foundation (built first, sequentially, on main thread; FROZEN interfaces)

### Task 1: Config layer
**Files:** Create `project_docs/config.py`, `tests/project_docs/test_config.py`; Modify `pyproject.toml`.

Frozen dataclasses mirroring the brief's TOML, every field defaulted to the brief's safe default. Top-level `ProjectDocsConfig` with nested `IngestionCfg, ProjectsCfg, ScannerCfg, ScannerDiscoveryCfg, PointerCfg, GitCfg, EmbeddingsCfg, McpCfg`. 

Public API (FROZEN):
```python
def load_config(start: Path | None = None, path: Path | None = None) -> ProjectDocsConfig: ...
def find_config_file(start: Path) -> Path | None: ...   # walk up for knowledge-engine.toml; honor KE_CONFIG_PATH
PROJECT_DOCS_DEFAULTS: dict   # canonical defaults for docs/tests
```
`ProjectDocsConfig.enabled` defaults False. Use `tomllib` if available else `tomli`. Missing file → all defaults.

- [ ] Test: absent file → `enabled is False`, `scanner.enabled is False`, `embeddings.enabled is False`, `mcp.default_result_mode == "summary"`.
- [ ] Test: a temp TOML with `[project_docs] enabled=true` overrides only that field; siblings keep defaults.
- [ ] Test: `find_config_file` finds a file two dirs up; returns None when absent.
- [ ] Implement; run `pytest tests/project_docs/test_config.py -v`; commit.

### Task 2: Paths
**Files:** Create `project_docs/paths.py`, `tests/project_docs/test_paths.py`.

FROZEN:
```python
def slugify(name: str) -> str: ...                       # reuse cli._slugify rules
def resolve_project_root(start: Path, cfg) -> Path: ...   # git toplevel else cfg else start
def project_docs_dir(root: Path, cfg) -> Path: ...        # root / cfg.database_dir
def project_db_path(root: Path, cfg, slug: str) -> Path: ...
def fingerprint_db_path(root: Path, cfg) -> Path: ...     # root / cfg.fingerprint_database
def canonical_root(root: Path) -> str: ...                # normalized, case-folded on win32
```
- [ ] Tests: slugify cases; canonical_root stable across separators; db paths under `.knowledge-engine/` by default.
- [ ] Implement; test; commit.

### Task 3: DB factory + migration runner
**Files:** Create `project_docs/db.py`, `tests/project_docs/test_db.py`.

FROZEN:
```python
def connect(path: Path) -> sqlite3.Connection: ...        # WAL, busy_timeout, FK on, Row factory
def apply_migrations(conn, migrations_dir: Path, only_prefixes: tuple[str,...] | None = None) -> int: ...
def schema_version(conn) -> int: ...
```
Migration runner: ensures `schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)`, applies `NNN_*.sql` files whose numeric prefix > current max, in order, each in a transaction, records version. `only_prefixes` lets the registry DB apply only `001_*` and a project DB apply `002..007`.
- [ ] Test: applying twice is idempotent (second call applies 0); version increases; a registry-only run applies just 001.
- [ ] Implement; test; commit.

### Task 4: Schema constants
**Files:** Create `project_docs/schema.py`, `tests/project_docs/test_schema.py`.

FROZEN string-enums (plain `str` constants grouped in frozensets):
```python
CATEGORIES = {"doc","devlog","qa","design_note","decision_record","test_plan",
              "test_log","build_log","runtime_log","diagnostic_log","docstring",
              "comment","git_meta","diff_summary","skill","agent_def","tool_def","reference"}
SANITIZATION_STATES = {"sanitized","raw_omitted","raw_retained","redacted",
                       "rejected_oversize","rejected_binary","rejected_unsafe"}
INGESTION_STATES = {"ingested","skipped_dedupe","rejected","pending"}
TEST_CLASSIFICATIONS = {"pass","fail","error","unknown"}
LINK_TYPES = {"parent","child","related","code_span"}
POINTER_TYPES = {"docstring","doc","testlog","buildlog","note"}
SCAN_MODES = {"report","ingest","pointer_plan","pointer_apply","embedding"}
```
- [ ] Test: membership sanity (e.g. `"test_log" in CATEGORIES`).
- [ ] Implement; test; commit.

### Task 5: Models
**Files:** Create `project_docs/models.py`, `tests/project_docs/test_models.py`.

FROZEN dataclasses (frozen=False, slots where helpful). Field names MUST match schema columns in §5 of the spec. Define: `DocRecord, IngestionRun, TestRun, LogRecord, Pointer, Candidate, ScanReport, GitContext, DiffSummary, EmbeddingMeta`. Each has `to_row()`/`from_row()` round-trip helpers for its primary table.
- [ ] Test: `DocRecord.from_row(d.to_row())` round-trips all fields.
- [ ] Implement; test; commit.

### Task 6: Hashing
**Files:** Create `project_docs/hashing.py`, `tests/project_docs/test_hashing.py`.
```python
def content_hash(text: str) -> str: ...   # sha256 hex
def short_hash(text: str, n: int = 8) -> str: ...
```
- [ ] Test: determinism; short length.
- [ ] Implement; test; commit.

### Task 7: Migrations (SQL files) + `__init__`
**Files:** Create `project_docs/migrations/001_project_fingerprints.sql` … `007_embeddings.sql`, `project_docs/__init__.py`, `tests/project_docs/test_migrations.py`.

DDL exactly per spec §5 (registry tables in 001; project tables split 002 core, 003 fts, 004 logs/tests, 005 pointers, 006 git, 007 embeddings). FTS5: `CREATE VIRTUAL TABLE project_docs_fts USING fts5(searchable_body, summary, content='', tokenize='porter unicode61');`
`__init__.py` exports `load_config, is_enabled, resolve_project_root` and `__all__`.
- [ ] Test: a fresh registry DB applies 001 and has `projects` table; a fresh project DB applies 002–007 and has `project_docs`, `project_docs_fts`, `test_runs`, `doc_pointers`, `git_context`, `doc_embeddings`.
- [ ] Implement; test; commit. **P0 interfaces are now FROZEN.**

---

## P1 — Identity + store + read (first vertical slice)

### Task 8: Sanitization
**Files:** Create `project_docs/sanitize.py`, `tests/project_docs/test_sanitize.py`.
```python
@dataclass
class SanitizeResult: text: str; status: str; redactions: int
def sanitize(text: str, cfg, *, content_kind: str = "doc") -> SanitizeResult: ...
```
Rules list `Rule(name, pattern, replacement)`: AWS-like keys, generic `*_API_KEY=`/`token=`/`secret=`, `Bearer <jwt>`, creds in URLs (`://user:pass@`), absolute home paths → `~`, optional email/PII (config), env-var assignments. Oversize (> `cfg.ingestion.max_document_bytes`) → status `rejected_oversize`. Non-text/binary → `rejected_binary`. `retain_raw_content=False` (default) means callers store sanitized only.
- [ ] Tests (synthetic secrets only): key redaction; URL creds; oversize rejection; clean text → `sanitized`, redactions 0.
- [ ] Implement; test; commit.

### Task 9: Fingerprints
**Files:** Create `project_docs/fingerprints.py`, `tests/project_docs/test_fingerprints.py`.
```python
def project_fp(canonical_root: str, remote_identity: str | None = None) -> str: ...   # "proj_"+b32(sha256)[:16]
def branch_fp(project_fp: str, branch_name: str) -> str: ...                          # "br_"+...
def ensure_project(conn, canonical_root, name, remote_identity=None, override=None) -> str: ...
def ensure_branch(conn, project_fp, branch_name, override=None) -> str: ...
def validate_context(conn, project_fp, branch_fp) -> None: ...   # raise ContextError if missing/mismatch
class ContextError(Exception): ...
```
`ensure_project` records in `project_fingerprints`/`projects`; on derived-fp collision with a different root, raise unless `override`. Every alloc/override/collision → `fingerprint_events`.
- [ ] Tests: determinism (same inputs → same fp); collision raises; retroactive branch alloc; validate passes after ensure, raises before.
- [ ] Implement; test; commit.

### Task 9b: Registry
**Files:** Create `project_docs/registry.py`, `tests/project_docs/test_registry.py`.
```python
def register_project(conn, root: Path, cfg, name=None, fingerprint=None) -> dict: ...
def list_projects(conn) -> list[dict]: ...
def list_branches(conn, project_fp) -> list[dict]: ...
def validate_project(conn, project_fp) -> dict: ...   # exists? db present? counts
def current_context(root: Path, cfg, conn) -> dict: ...  # project_fp, branch, branch_fp, git presence
```
- [ ] Tests: register then list; current_context returns detected branch (mock git) and allocated fps.
- [ ] Implement; test; commit.

### Task 10: Ingestion pipeline (direct)
**Files:** Create `project_docs/ingest.py`, `tests/project_docs/test_ingest.py`.
```python
def ingest_record(project_conn, registry_conn, *, project_fp, branch_fp, source_path, category,
                  subtype, text, cfg, source_uri=None, source_modified_at=None,
                  git=None, run_id=None, summarizer=None) -> DocRecord: ...
def begin_run(project_conn, registry_conn, project_fp, branch_fp, mode) -> str: ...
def finish_run(project_conn, run_id, stats: dict, status="completed") -> None: ...
```
Stages exactly per spec §7. Dedupe by `(content_hash, source_path, branch_fp)` → `INGESTION_STATES.skipped_dedupe`. Writes `project_docs` + `project_doc_bodies` (+raw only if `retain_raw_content`) + `project_doc_provenance`, then FTS upsert (rowid = `project_docs.rowid`). Pointer id pre-allocated via `pointers.new_pointer_id` (P4 dependency: define a stub returning a stable id in P1, real allocation in P4) — to avoid cross-phase coupling, P1 uses `pointers.format_pointer(type, project_fp, branch_fp, record_id)` which is grammar-only and lives in P4's `pointers.py`; **define `pointers.format_pointer` in Task 19 but stub it here only if P4 not yet merged.** (Build order keeps P4 before final integration, so import directly.)
- [ ] Tests: ingest one doc → row present, FTS finds it; re-ingest identical → dedupe; validate_context called (mismatch raises).
- [ ] Implement; test; commit.

### Task 11: Search
**Files:** Create `project_docs/search.py`, `tests/project_docs/test_search.py`.
```python
def search(project_conn, query: str, *, limit=10, branch_fp=None, category=None,
           source_path=None, git_commit=None, since=None, mode="summary") -> list[dict]: ...
def get_record(project_conn, record_id, *, mode="summary", cfg=None) -> dict | None: ...
def search_by_path(...); search_by_type(...); search_by_branch(...); search_recent(...)
```
FTS5 MATCH on `project_docs_fts`, join `project_docs` for filters; bm25 order; snippet for summary. `mode="full"` returns body only when `cfg.mcp.allow_full_content`.
- [ ] Tests: filter by branch/category narrows; full mode gated.
- [ ] Implement; test; commit.

### Task 12: MCP tool base + registry/query/capability tools + server wiring
**Files:** Create `project_docs/mcp_tools/base.py`, `registry_tools.py`, `query_tools.py`, `capability_tools.py`, `__init__.py`; Modify `mcp_server.py`, `cli.py`; Tests `tests/project_docs/test_mcp_tools.py`, `tests/test_mcp_server_merge.py`.

`base.ToolModule`: `tools() -> list[dict]`, `dispatch(name, args, ctx) -> dict`. `collect_tools(cfg)` returns merged defs + dispatch map for enabled groups. `mcp_server.Server` builds `TOOLS = BASE_TOOLS + project_docs tools` and routes unknown names to the project_docs dispatch. Existing 4 tools unchanged. Capability tools: `project_docs.capabilities/config_status/healthcheck/explain_available_tools` read config + DB presence; never error when features off.
- [ ] Tests: existing `search`/`registry_list` still listed; `project_docs.capabilities` returns gate states; disabled group → its tools absent or return `{"status":"disabled"}`; full-content tool without gate → `{"status":"not_permitted"}`.
- [ ] Implement; test; commit. **P1 slice complete: end-to-end register→ingest→search→MCP read.**

---

## P2 — Scanner (report + ingest)

### Task 13: Scanner base + discovery + validators
**Files:** Create `scanner/base.py`, `scanner/discovery.py`, `scanner/validators.py`; tests.
`Detector` ABC `discover(root, cfg) -> Iterable[Candidate]`; `discovery.walk(root, cfg)` honors `respect_gitignore`, `follow_symlinks=False`, `max_file_bytes`. `validators.preflight(mode, cfg, conn, project_fp, branch_fp)` enforces gates.
- [ ] Tests: walk skips gitignored + oversize + symlinks; preflight blocks ingest when `scanner.enabled` False.
- [ ] Implement; test; commit.

### Task 14: Detectors (markdown, docstrings, comments, logs)
**Files:** Create `scanner/markdown.py`, `scanner/docstrings.py`, `scanner/comments.py`, `scanner/logs.py`; tests on fixture project.
Each returns `Candidate(source_path, category, subtype, est_bytes, risk_flags, span, preview)`. docstrings: python `ast`-based module/class/func docstring spans. comments: off unless `include_structured_comments`.
- [ ] Tests: markdown detector finds `docs/*.md` + devlog; docstring detector finds a func docstring span in fixture; comments yields nothing when disabled.
- [ ] Implement; test; commit.

### Task 15: Report + ingest modes + scanner tools
**Files:** Create `scanner/report.py`, `scanner/ingest.py`, `mcp_tools/scanner_tools.py`; Modify `mcp_tools/__init__.py`, `cli.py`; tests.
`report.run(root, cfg, conn) -> ScanReport` (no writes). `ingest.run(...)` preflight → pipeline. Tools: `scanner_report/scanner_ingest/scanner_status/scanner_validate` (+`scanner_plan_pointers/scanner_apply_pointers` registered in P4).
- [ ] Tests: report on fixture lists candidates + recommended actions, writes nothing (DB row count unchanged); ingest writes when enabled.
- [ ] Implement; test; commit.

---

## P3 — Logs/tests + Git

### Task 16: Logs/tests store
**Files:** Create `project_docs/logs.py`; tests.
```python
def record_test_run(project_conn, registry_conn, cfg, *, command, exit_code, started_at,
                    duration_ms=None, framework=None, target=None, output="", git=None,
                    project_fp, branch_fp) -> TestRun: ...
def record_build_log(...); def record_runtime_log(...)
def get_test_history(...); get_failure_context(...); get_latest_test_summary(...)
```
Classify pass/fail/error from exit_code + output. Sanitize output; raw only if enabled.
- [ ] Tests: classification table; history ordering; latest summary; raw omitted by default.
- [ ] Implement; test; commit.

### Task 17: Git context + log/git tools
**Files:** Create `project_docs/git_context.py`, `mcp_tools/log_tools.py`, `mcp_tools/git_tools.py`; Modify `__init__.py`, `cli.py`; tests.
`git_context.collect(root, cfg) -> GitContext | None` via `subprocess.run([...], shell=False)`; returns None if no git/binary. `diff_summary(root, a, b)` numstat. Full diffs only if `git.include_full_diffs`. Tools: `search_test_logs/search_build_logs/search_runtime_logs/get_test_history/get_failure_context/get_latest_test_summary` and `git_context/search_by_commit/search_by_diff/get_branch_lineage/get_change_context/explain_file_history`.
- [ ] Tests: collect returns None when git absent (mock); runtime-log tools return `{"status":"disabled"}` when off; diff tools gated.
- [ ] Implement; test; commit.

---

## P4 — Pointers

### Task 18: Pointer grammar + store
**Files:** Create `project_docs/pointers.py`; tests.
```python
def format_pointer(ptype, project_fp, branch_fp, record_id) -> str: ...
def parse_pointer(uri: str) -> dict: ...   # handles ke-doc:// and KE-DOCSTRING:// alias
def allocate(project_conn, record_id, ptype, project_fp, branch_fp, source_path, span, content_hash) -> str: ...
def resolve(project_conn, uri, *, mode="summary", cfg=None) -> dict | None: ...
def list_pointers(...); validate_pointer(...); pointer_backrefs(...)
```
- [ ] Tests: format/parse round-trip; `KE-DOCSTRING://` alias parses to `type=docstring`; resolve returns summary, full gated.
- [ ] Implement; test; commit.

### Task 19: Pointer plan/apply + tools (guarded mutation)
**Files:** Create `scanner/pointer_plan.py`, `scanner/pointer_apply.py`, `mcp_tools/pointer_tools.py`; Modify `scanner_tools.py`, `__init__.py`, `cli.py`; tests.
`pointer_plan.run(...)` → `pointer_rewrite_plans` row + preview items, NO writes to source. `pointer_apply.run(plan_id, ...)` requires `pointer_replacement.enabled` AND `allow_source_mutation` AND non-dry-run; writes backup, replaces span with stub (spec §6), validates pointer resolves, records `pointer_rewrite_events`, supports rollback. Tools: `resolve_pointer/list_pointers/validate_pointer/pointer_backrefs` + `scanner_plan_pointers/scanner_apply_pointers`.
- [ ] Tests: plan writes no source changes; apply blocked without gates; apply with gates on a temp copy replaces span + backup exists + rollback restores; audit event recorded.
- [ ] Implement; test; commit.

---

## P5 — Embeddings (optional)

### Task 20: Providers + index + tools
**Files:** Create `embeddings/providers.py`, `embeddings/local.py`, `embeddings/remote.py`, `embeddings/index.py`, `mcp_tools/embedding_tools.py`; Modify `__init__.py`, `cli.py`; tests.
`EmbeddingProvider.embed(texts) -> list[list[float]]`; `local.OllamaProvider` (reuse `embeddings/build.py` struct-pack + cosine), `remote.RemoteProvider` (raises unless `allow_remote_provider`). `index.generate(project_conn, provider, ...)`, `index.semantic_search(...)`. Tools: `embedding_status/generate_embeddings/refresh_embeddings/semantic_search/similar_records/cluster_records` — all return `{"status":"disabled"|"not_configured"}` when off.
- [ ] Tests: status reports disabled by default; semantic_search gated by `allow_embedding_search`; provider with stub vectors stores + retrieves (no network).
- [ ] Implement; test; commit.

---

## P6 — Docs, README, governance

### Task 21: Product docs + README + fixtures
**Files:** Create `project_docs/docs/PROJECT_DOCS.md`, `PROJECT_DOCS_CONFIG.md`, `PROJECT_DOCS_SCANNER.md`, `PROJECT_DOCS_MCP_TOOLS.md`, `PROJECT_DOCS_POINTERS.md`; `tests/fixtures/sample_project/` (sanitized); Modify root `README.md`, `CHANGELOG.md`, `CATALOG.md`, `scripts/mcp-client-config.example.json` (note new tools).
- [ ] Verify all config keys documented; pointer grammar documented + matches `pointers.py`.
- [ ] Commit.

### Task 22: Full verification
- [ ] `ruff check .` clean; `pytest -q` green (git/embedding tests skip cleanly when binary/provider absent).
- [ ] `knowledge-engine project-docs --help` works; MCP `tools/list` includes project_docs tools; existing 4 tools intact.
- [ ] Commit; summarize.

---

## Self-Review

**Spec coverage:** §3 config→T1; §4 fingerprints→T9; §5 schema→T7; §6 pointers→T18–19; §7 pipeline→T10; §8 scanner modes→T13–15,19,20; §9 MCP→T12,15,17,19,20; §10 sanitize→T8; §11 tests→all; logs/tests first-class→T16; git/diffs→T17; embeddings→T20; capability tools→T12. All brief tool groups mapped.

**Placeholder scan:** none — every task names exact files, signatures, and test assertions.

**Type consistency:** model field names pinned to spec §5 columns (T5); pointer `format_pointer/parse_pointer` names reused in T10/T18/T19; `validate_context` name reused T9/T10/T13; config gate names reused across MCP tasks. One cross-phase note resolved: `pointers.format_pointer` is grammar-only and may be imported by T10 since P4 lands before final integration.
