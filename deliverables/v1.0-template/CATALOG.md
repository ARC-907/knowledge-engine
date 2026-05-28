---
title: Knowledge-Engine — Catalog
status: v1.0
created: 2026-05-11
last_updated: 2026-05-23
---

# Catalog

> Inventory of what ships in v1.0. The corpus subdirectories ship empty by design (placeholder `EMPTY.md` only). Drop your own content in.

## Corpus

### Libraries (`corpus/libraries/`)

**3 curated demonstration libraries** bundled — 42 markdown files total — chosen to show the three-lens organizational methodology applied across distinct domains (analytical / business / engineering). Each library = its `README.md` + `CATALOG.md` + first 3 numbered folders:

| Library | Three-lens framework | Files | Why it's bundled |
|---|---|---:|---|
| **Decision Analysis** | FRAME → ANALYZE → DECIDE | 11 | Universal-utility — every reader faces decisions under uncertainty. Demonstrates the analytical-framework variant of the methodology (OODA / MDMP / Cynefin / pre-mortem). |
| **AI Monetization** | IDENTIFY → EVALUATE → EXECUTE | 21 | High-relevance for the dev-and-AI audience. Demonstrates the business-strategy variant of the methodology. |
| **System Design** | FRAME → DESIGN → EVOLVE | 10 | Direct-utility for the engineering audience. Demonstrates the design-work-product variant of the methodology. |

The three-lens framework itself is the gem — the proprietary methodology that separates slop research from usable research, and that keeps a high-churn corpus navigable.

### Skills (`corpus/skills/`)

_Ships empty — buyer-supplied. See `corpus/skills/EMPTY.md`._

### Kits (`corpus/kits/`)

_Ships empty — buyer-supplied. See `corpus/kits/EMPTY.md`._

### Capabilities (`corpus/capabilities/`)

_Ships empty — buyer-supplied. See `corpus/capabilities/EMPTY.md`._

### Governance (`corpus/governance/`)

_Ships empty — buyer-supplied. See `corpus/governance/EMPTY.md`._

### Schemas (`corpus/schemas/`)

Bundled JSON schemas for common research-data shapes. Pure-data schemas; useful as templates when building your own workflows. 10 files.

- `agent-message.schema.json`
- `audit-finding.schema.json`
- `calendar-event.schema.json`
- `cluster-record.schema.json`
- `conflict-record.schema.json`
- `handoff-bundle.schema.json`
- `research-object.schema.json`
- `source-record.schema.json`
- `task.schema.json`
- `tool-card.schema.json`

### Prompts (`corpus/prompts/`)

Bundled agnostic prompt templates with `{placeholder}` variables. 12 files.

- `audit-consistency-check.md`, `audit-static-analysis.md`, `audit-test-generation.md` — code-audit prompts
- `code-decompose-module.md`, `code-fix-test.md`, `code-test-single-function.md` — code-modification prompts
- `extraction-worker.md` — generic extraction
- `research-extract.md`, `research-normalize.md`, `research-synthesize.md`, `research-web-search.md` — research workflow
- `synthesis-agent.md` — cloud synthesis agent (preserves library structure, draws only from handoff bundle)

### Samples (`corpus/samples/`)

Seed content used by first-run demo and tests. Pure synthetic, zero PII.

- `demo-library/` — minimal library (4 files): `README.md`, `CATALOG.md`, `00-foundations/overview.md`, `01-applied/example.md`
- `demo-skill/` — minimal skill (2 files): `SKILL.md`, `procedure.md`
- `assets/.gitkeep` — placeholder for binary asset folder

## Engine modules

### Core package (`engine/src/knowledge_engine/`)

- `app.py` — FastAPI factory
- `cli.py` — `knowledge-engine` console script (`info | reindex | search | serve | bootstrap | watch | mcp`)
- `config.py` — env-driven path/URL config (`KE_*`, `OLLAMA_BASE_URL`)
- `indexer.py` — SQLite FTS5 indexer with `bm25` ranking + snippet highlighting
- `mcp_server.py` — JSON-RPC 2.0 MCP stdio server (protocol `2024-11-05`)
- `registry.py` — JSON-backed registry + SQLite mirror
- `watcher.py` — `watchdog`-based auto-register
- `api/` — FastAPI route modules: `health_routes`, `registry_routes`, `search_routes`, `generate_routes`
- `embeddings/` — opt-in bge-m3 embedding index + cosine search (build.py + search.py)
- `routing/` — provider abstraction: `EchoProvider`, `CloudHTTPProvider`, `OllamaProvider`
- `sandbox/` — `NoopSandbox` scaffold + `get_sandbox()` factory hook
- `foundation/` — opt-in pipeline foundation: `config.py` (YAML loader) + `db.py` (SQLite WAL backbone, schema auto-create)
- `pipeline/` — opt-in multi-worker pipeline: `queue.py`, `message_board.py`, `worker_registry.py`, `task_classifier.py`
- `tools/` — opt-in tool host: `host.py` (HTTP-addressable script/service/static tool registry)

### Optional adjuncts

- `engine/routing_local/ollama_provider.py` — opt-in Ollama provider (imported by `routing/` when reachable)
- `engine/sandbox_adapter/` — opt-in sandbox scaffold (README only — bring your own executor)

### Tests (`engine/tests/`)

- `test_smoke.py` — 4 tests: registry CRUD roundtrip; indexer rebuild + search; FastAPI factory + `/health` + `/info`; search/reindex/registry routes under the worker threadpool (SQLite cross-thread regression guard).

## Dashboard

- `ui/index.html` — single-file Alpine.js + Tailwind dashboard auto-mounted at `/ui/`

## Scripts

- `scripts/install.ps1` / `install.sh` — venv + editable install
- `scripts/serve.ps1` / `serve.sh` — KE_* env + uvicorn launch on port 9210
- `scripts/mcp-client-config.example.json` — MCP client wiring template

## Documentation

Top-level docs:

- `README.md` — buyer-facing overview + quickstart
- `ARCHITECTURE.md` — three-layer model, component map, HTTP + MCP surface
- `DEPENDENCIES.md` — required-core / optional-feature / dev-only
- `CHANGELOG.md` — version history
- `RESEARCH-LOG.md` — corpus-research session log (initialized empty for buyer use)
- `CATALOG.md` — this file
- `LICENSE` — MIT (engine + dashboard + demo content)
- `LICENSE-BUYER.md` — single-buyer commercial-permissive (paid Standard bundle)

Buyer guides (`docs/`):

- `docs/README.md` — guide index
- `docs/QUICKSTART.md` — install to working dashboard in ~10 minutes
- `docs/BRING-YOUR-OWN-CORPUS.md` — replace the demo libraries with your content
- `docs/LIBRARY-AUTHORING.md` — author a library with the three-lens framework
- `docs/SKILL-AUTHORING.md` — add skill packages
- `docs/MCP-WIRING.md` — wire the MCP server into Claude Desktop / Cursor / Continue
- `docs/EMBEDDINGS.md` — opt-in bge-m3 semantic search
- `docs/DEPLOYMENT.md` — run beyond localhost; hardening checklist
- `docs/THEMING.md` — restyle or replace the dashboard
- `docs/FAQ.md` — common questions
