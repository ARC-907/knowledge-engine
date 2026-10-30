# Scoped knowledge: workspaces, projects, collections and classification

Knowledge-Engine can record *where* each piece of content belongs and *what*
it is, without moving or copying it: workspaces, projects, collections,
research types, tags and curation status. This metadata lives in its own
SQLite store, `knowledge.db`, next to the search index. It is organisation,
not access control: anyone who can reach the local-trust API can read and
change it.

Nothing here changes `search`. The `/search` route, the MCP `search` tool and
`knowledge-engine search` keep returning exactly what they did before.
Scoped search across sources uses this metadata through the retrieval broker:
see [KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md).

## The model

| Dimension | What it is | Notes |
| --- | --- | --- |
| Workspace | An explicit knowledge boundary | Stable id (`default` after migration); the display name may change |
| Project | An optional narrower context | Opaque id; two projects may share a display name |
| Source | The system that owns records | `corpus:base` is the corpus registry |
| Collection | A curated grouping | A record may be in many collections |
| Research type | `reference`, `paper`, `article`, `transcript`, `code`, `note`, `dataset`, `synthesis`, `other` | Unknown values are rejected |
| Tags | Free labels | Normalised: lower case, spaces become `-` |
| Curation status | `unclassified`, `inbox`, `reviewed`, `archived` | Never set automatically |

## Migrating an existing corpus

The migration registers every indexable corpus file (`.md`, `.txt`, `.rst`
under each registry entry, enabled or not) as a record of the source
`corpus:base` in the default workspace. Every record starts as research type
`other`, status `unclassified`, in no project and no collection. Files are
not moved, copied or re-indexed.

1. **Dry run** (reads only; creates nothing):

   ```bash
   knowledge-engine knowledge migrate --dry-run
   ```

   The JSON report lists the number of records to create, files reached from
   several nested registry entries (they become one record), entries whose
   folder is missing, files that are not UTF-8, records whose file has
   disappeared, an existing checkpoint to resume, and the disk space needed.

2. **Apply**:

   ```bash
   knowledge-engine knowledge migrate --apply [--backup-dir DIR] [--batch-size 500] [--max-batches N]
   ```

   It first writes a backup directory `knowledge-migrate-<timestamp>/`
   (default under `<KE_DATA_DIR>/backups/`) with a copy of `knowledge.db` (if
   it existed), `registry.json` and a `manifest.json`. It then creates the
   default workspace and the `corpus:base` source and inserts records in
   batches. Each batch commits together with its checkpoint, so an
   interrupted run (or one stopped with `--max-batches`) continues where it
   left off the next time you run `--apply`. Running it again later only adds
   new files; it never duplicates records or overwrites your classification.

3. **Check**:

   ```bash
   knowledge-engine knowledge status
   ```

### Restoring the backup

Stop the engine, copy `knowledge.db` from the backup directory over the
configured store (`KE_KNOWLEDGE_DB`, default `<KE_DATA_DIR>/knowledge.db`) and
delete any `knowledge.db-wal` / `knowledge.db-shm` next to it, then restart.
If the manifest says `knowledge_db_existed: false`, delete `knowledge.db`
instead. Changes made after the backup are lost. Do not run an older engine
against a newer `knowledge.db`: it refuses to open it (`incompatible`); restore
the backup taken before the upgrade instead.

## Organising content

Every operation is available on three surfaces with the same JSON:
the `knowledge-engine knowledge ...` CLI, HTTP routes under `/knowledge`, and
MCP tools named `knowledge_*`.

```bash
knowledge-engine knowledge project create default "Thesis"          # prints the new project_id
knowledge-engine knowledge collection create default "Methods" --id methods
knowledge-engine knowledge record list --tag draft                  # default workspace
knowledge-engine knowledge collection add methods --record corpus:base <record_id>
knowledge-engine knowledge record classify corpus:base <record_id> --type paper --status reviewed --add-tag ml
knowledge-engine knowledge record classify corpus:base <record_id> --project <project_id>
```

| HTTP | Purpose |
| --- | --- |
| `GET /knowledge/status` | Counts and last migration run |
| `GET`/`POST /knowledge/workspaces` | List / create workspaces |
| `GET`/`POST /knowledge/workspaces/{id}/projects` | List / create projects |
| `GET /knowledge/workspaces/{id}/sources` | List sources |
| `GET`/`POST /knowledge/workspaces/{id}/collections` | List / create collections |
| `POST /knowledge/collections/{id}/members`, `…/members/remove` | Change membership |
| `PATCH /knowledge/records/{source_id}/{record_id}` | Classify a record |
| `POST /knowledge/records/query` | List the records in a scope |
| `GET /knowledge/workspaces/{id}/profiles`, `PUT …/profiles/{profile_id}` | Scope profiles |
| `PUT`/`DELETE /knowledge/workspaces/{id}/project-context/links/{project_id}` | Link a project to a project-context store ([KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md#project-context-sources)) |

### Scope rules

`record list` (and `POST /knowledge/records/query`) take a query context:

- `workspace_id` is required (the CLI defaults to the default workspace).
- An omitted selection uses the profile (`--profile`, else
  `KE_KNOWLEDGE_PROFILE`; the built-in `default` profile means "every source,
  no other filter").
- An explicitly empty source, collection or research-type list matches
  nothing; an empty tag list means "no tag filter".
- Several values of one kind are alternatives (OR); several tags must all be
  present (AND); different kinds combine with AND.
- With a project, only that project's records match; add `include_workspace`
  (`--include-workspace`) to include records that belong to no project.
- An unknown id is an error (`unknown_scope`), never an empty or global result.

### Errors

Failures carry a stable code: `invalid_scope`, `unknown_scope`,
`invalid_input`, `denied`, `unavailable`, `incompatible`, `timeout`,
`cancelled`, `output_invalid`, `internal_error`. HTTP answers
`{"detail", "type", "error": {"code", "message", "details", "retryable"}}`;
MCP returns `{"error": {...}}` as an error result; the CLI prints
`error[<code>]: <message>` and exits non-zero (2 for scope and input errors).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `KE_KNOWLEDGE_ENABLED` | `1` | `0` removes the `/knowledge` routes (including search and open), the `knowledge_*` MCP tools and `knowledge.search`/`knowledge.open` (the CLI still works) |
| `KE_KNOWLEDGE_DB` | `<KE_DATA_DIR>/knowledge.db` | The metadata store |
| `KE_KNOWLEDGE_DEFAULT_WORKSPACE` | `default` | Workspace the migration creates |
| `KE_KNOWLEDGE_PROFILE` | `default` | Profile for omitted selections |

See [CONFIGURATION.md](CONFIGURATION.md) for the full reference. Exposing these
routes beyond localhost needs the same hardening as the rest of the API
([DEPLOYMENT.md](DEPLOYMENT.md)).

To keep the exact bytes of corpus files as revisions, open cited spans and
export checked citations, continue with [CITATIONS.md](CITATIONS.md).
