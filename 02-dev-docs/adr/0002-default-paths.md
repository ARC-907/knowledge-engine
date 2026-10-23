# ADR 0002 — Default paths from `KE_HOME`, runtime data under `.knowledge-engine/data`

- **Status:** Accepted (3.0.0)
- **Date:** 2026-10

## Context

Before 3.0, path defaults were computed in several places relative to the
installed module: the FTS index landed in `engine/data` (in practice
`engine/src/data` after the package was layered), the corpus resolved to the
nonexistent `engine/corpus` when no `KE_*` variable was set, the pipeline
database defaulted to a cwd-relative path, and the project-context subsystem
already used a `.knowledge-engine/` directory of its own. Wheels installed
into `site-packages` had no sensible place to write at all. Blank `KE_*`
values were treated as set.

## Decision

One resolver, `knowledge_engine.core.paths`, derives every default:

- `KE_HOME` — explicit, else the nearest directory at or above the working
  directory containing `corpus/registry.json`, else the working directory.
- `KE_CORPUS_ROOT` = `<home>/corpus`; `KE_REGISTRY_PATH` =
  `<corpus_root>/registry.json`; `KE_DATA_DIR` =
  `<home>/.knowledge-engine/data`.
- Blank values count as unset. Resolution is read-only; entry points that
  write state create the data directory explicitly.
- The FTS index, embeddings index, pipeline/board database, board scope
  databases and the master-key file all live under the data directory. The
  never-read SQLite mirror of the registry (`registry.db`) is dropped.
- Every environment variable is declared on `core.settings.Settings`, which
  also generates `01-user-docs/CONFIGURATION.md`.

## Consequences

- Breaking: existing installs must set `KE_DATA_DIR` to the old location or
  rebuild their indexes (documented in the 3.0.0 migration guide).
- A checkout works from any subdirectory; a `uv tool install` works from any
  directory that contains `corpus/registry.json`, or with `KE_HOME` set.
- MCP client configs need one variable (`KE_HOME`) instead of three paths.
- The Docker image sets `KE_HOME=/data` and mounts the corpus and the data
  volume under it.
- `.knowledge-engine/` is gitignored at every level.
