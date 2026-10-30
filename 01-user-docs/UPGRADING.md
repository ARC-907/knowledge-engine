# Upgrading from 3.0 to 3.1

Version 3.1 adds a set of opt-in features on top of 3.0 that the roadmap calls
"V2". Everything that worked in 3.0 keeps working unchanged:

- `/search`, the MCP `search` tool and `knowledge-engine search` return
  byte-for-byte what they did (pinned by tests);
- `registry.json`, the FTS index, the embedding index, the agent board and
  project context keep their formats;
- existing settings keep their names and defaults.

The new features keep their state in two **new** SQLite files next to the
index, `knowledge.db` and `modules.db`, which 3.0 never reads. Nothing is
migrated in place.

| Feature | What it adds | Guide |
| --- | --- | --- |
| Scoped knowledge | Workspaces, projects, collections, research types, tags | [SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md) |
| Retrieval broker | Scoped search and exact-revision open across sources | [KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md) |
| Evidence and citations | Recorded revisions, evidence spans, Markdown/CSL-JSON citations | [CITATIONS.md](CITATIONS.md) |
| Modules | Operator-registered utilities through one dispatcher; ingestion as evidence | [MODULES.md](MODULES.md) |
| Serena (optional) | Read-only code symbols from a separately installed Serena | [SERENA.md](SERENA.md) |
| Dashboard | Knowledge and Modules tabs | [DASHBOARD.md](DASHBOARD.md) |
| JSON logs | `KE_LOG_FORMAT=json`, `KE_LOG_FILE`, a Loki/Grafana stack | [DEPLOYMENT.md](DEPLOYMENT.md#logs) |

## Before you upgrade

1. Stop the engine (server, MCP clients, `watch`).
2. Back up the data directory (`KE_DATA_DIR`, default
   `<KE_HOME>/.knowledge-engine/data`) and your corpus. 3.1 does not change
   files 3.0 uses, but a backup makes rollback trivial.

## Upgrade

1. Install 3.1 the way you installed 3.0 (see [QUICKSTART.md](QUICKSTART.md)),
   or in a checkout: `git pull && uv sync --project engine --locked`.
2. Start it once and check `knowledge-engine info` and `GET /health` (the
   version reads `3.1.0`). Nothing else is required: the existing search,
   dashboard and MCP tools work as before.

## Turning on the new features

All of these are optional and can be done later, in any order.

1. **Register the corpus as scoped knowledge** (creates `knowledge.db`,
   with a backup and resumable batches):

   ```bash
   knowledge-engine knowledge migrate --dry-run
   knowledge-engine knowledge migrate --apply
   ```

   Then organise records into projects and collections from the CLI, HTTP,
   MCP or the dashboard's Knowledge tab.

2. **Record revisions** so hits can be opened at an exact revision and cited
   after the files change:

   ```bash
   knowledge-engine knowledge snapshot --apply
   ```

   Re-run it after corpus edits (or on a schedule). Revision bytes are kept
   according to `KE_EVIDENCE_RETENTION` and `KE_EVIDENCE_MAX_BYTES`.

3. **Modules**: set `KE_MODULES_DIR` to an operator directory, copy or write a
   module there (the reference modules are in `examples/modules/`), then
   `knowledge-engine module register <path>` and `module enable <id>`.
   Ingested output becomes searchable evidence of a `module:<id>` source.

4. **Serena**: install Serena separately, set the adapter settings and link a
   project ([SERENA.md](SERENA.md)). It is disabled by default.

5. **Structured logs**: `KE_LOG_FORMAT=json` and optionally `KE_LOG_FILE`;
   `deploy/observability/` ships them to Loki and Grafana.

Every new setting is listed in [CONFIGURATION.md](CONFIGURATION.md).
`KE_KNOWLEDGE_ENABLED=0` and `KE_MODULES_ENABLED=0` switch the new surfaces
off entirely.

## Rolling back to 3.0

1. Stop the engine and reinstall 3.0.
2. Nothing else is required: 3.0 ignores `knowledge.db`, `modules.db`,
   `module-artifacts/` and the new settings. Leave them in place to return to
   3.1 later, or delete them to discard the scoped metadata, evidence and
   module catalog.
3. If you upgrade again later, 3.1 picks those files up where it left them.
   Restoring an older `knowledge.db` from a `knowledge migrate` backup is
   described in [SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md#restoring-the-backup).

Do not open a `knowledge.db` written by a newer engine with an older 3.1.x
build: the store refuses schema versions it does not know rather than
guessing.
