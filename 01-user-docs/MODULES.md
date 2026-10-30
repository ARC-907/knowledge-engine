# Modules: executable utilities through one catalog

A **module** wraps a program you already run (a fetcher, a transcriber, an
internal HTTP service, an external MCP server) so agents and operators can
call it through Knowledge Engine's CLI, admin HTTP API and MCP server. One
dispatcher applies the same validation, policy, limits, audit and error codes
on every surface.

Modules never come from the corpus. A corpus entry of kind `tool` is
documentation, never permission to execute anything.

## Contents

- [Trust boundary](#trust-boundary)
- [Operator guide](#operator-guide)
- [Surfaces](#surfaces)
- [Limits, policy and errors](#limits-policy-and-errors)
- [Jobs](#jobs)
- [Artifacts and ingestion](#artifacts-and-ingestion)
- [Module author guide](#module-author-guide)
- [Reference modules](#reference-modules)

## Trust boundary

A module is **not sandboxed** because it is a module. Script and stdio MCP
modules run as the engine's OS user, in their module directory, in their own
process group. They get only `PATH`, locale and temp-dir variables, their
declared secret references and two call identifiers. They do **not** get
the engine's environment. Register only code you would run yourself. If you
need isolation, run the engine, or the module behind a `service`/`mcp`
transport, in a container or VM.

Execution needs four separate operator actions:

1. **Install.** Put the module directory under `KE_MODULES_DIR`, or anywhere
   outside the corpus.
2. **Register.** The manifest is validated as data. Nothing is executed.
3. **Configure.** Supply settings and secret *references*.
4. **Enable.** The engine checks compatibility, configuration, capability
   grants and name collisions.

Discovery never imports module code, starts a process or touches the network.

## Operator guide

```bash
export KE_MODULES_DIR=/srv/ke-modules          # one sub-directory per module
knowledge-engine module discover               # validate every manifest there (no execution)
knowledge-engine module register /srv/ke-modules/news-fetcher
knowledge-engine module configure example.news --set allow_live_http=false
knowledge-engine module enable example.news    # grants: declared ∩ host policy
knowledge-engine module invoke example.news fetch --input '{"url": "file:///srv/ke-modules/news-fetcher/fixtures/solar-storage.html"}'
knowledge-engine module status example.news --probe
knowledge-engine module disable example.news   # rejects new work, cancels active calls
```

- **Settings.** `--set NAME=VALUE` works for declared settings only. A
  `string_list` setting takes a comma-separated value.
- **Secrets.** `--secret NAME=ENV_VAR` names the host environment variable
  that holds a secret's value. The default is the manifest's `env`. Values
  are never stored. They are read when the module is called, and scrubbed
  from results, errors and logs. A setting value that looks like a
  credential is rejected.
- **Grants.** `--grant read --grant process ...` grants capabilities. Grants
  must be declared by the manifest and allowed by
  `KE_MODULES_HOST_CAPABILITIES` (default `read,network,process`; add
  `write` deliberately). The transport's own need (`process` for local
  programs, `network` for services) must be granted.
- **Re-registering.** Registering a changed module again disables it. Enable
  it again to re-run the checks. Its configuration is kept.
- **Restarts.** MCP clients see module tools as they were when the server
  started (ADR 0009). After enabling, disabling or re-registering, restart
  the MCP server or app to change the advertised list. A disabled module
  answers `unavailable` at once. A re-registered one answers `unavailable`
  with `restart_required` until the restart.
- **Failures.** A module that fails its checks at startup is `failed` and
  unavailable. Core search and every other module keep working.

Settings: `KE_MODULES_ENABLED`, `KE_MODULES_DIR`, `KE_MODULES_DB`,
`KE_MODULES_HOST_CAPABILITIES` ([CONFIGURATION.md](CONFIGURATION.md)).

### Lifecycle

| State | Meaning |
| --- | --- |
| `registered` | Manifest installed, never configured |
| `disabled` | Configured or disabled; calls answer `unavailable` |
| `starting` | Enable/startup checks running |
| `ready` | Enabled and checked |
| `degraded` | Enabled, but the last health probe failed |
| `failed` | Enable/startup checks failed; calls answer `unavailable` |
| `stopped` | The host shut down |

The enabled flag and the configuration are *intent*. The state is *observed*
and is rebuilt at every startup.

## Surfaces

| Surface | How |
| --- | --- |
| CLI | `knowledge-engine module discover|register|configure|enable|disable|list|status|invoke|job` (JSON on stdout; typed errors exit with the codes of the knowledge CLI) |
| HTTP | `/modules/*`: trusted peer **and** an `X-Board-Key` holding `board:admin`, like `/board/admin`. `POST /modules/register` takes `{"directory": "<name inside KE_MODULES_DIR>"}`; free paths are CLI-only. |
| MCP | Every granted tool of an enabled module is a tool named `<module_id>.<tool>` with the manifest's input schema. Calls run in the default workspace (`KE_KNOWLEDGE_DEFAULT_WORKSPACE`) and never ingest. |

HTTP invoke body:

```json
{"input": {...}, "scope": {"workspace_id": "research", "project_id": "p1"},
 "timeout_ms": 5000, "effects": ["read"], "ingest": false}
```

## Limits, policy and errors

Each call validates input size and schema and resolves the scope. It then
intersects the requested `effects` with the grants and the host policy.
`effects: ["read"]` makes the call read-only: a `write` tool is `denied`.
The call takes one of the module's `concurrency` slots, or fails with a
retryable `unavailable`. It runs under the module's `deadline_ms`; a caller
can lower the deadline but not raise it. The output must fit
`max_output_bytes` and match the output schema. Artifacts must fit
`max_artifacts` and `max_artifact_bytes`.

Only tools declared `idempotent` are retried, at most `limits.max_retries`
times, and only after `timeout`/`unavailable`.

| Code | Typical cause |
| --- | --- |
| `invalid_input` | Schema mismatch, input too large, unknown module/tool/setting, bad manifest |
| `invalid_scope` / `unknown_scope` | Malformed scope; an artifact or job not in your workspace |
| `denied` | Host policy, grants, refused argv/URL, module dir inside the corpus |
| `unavailable` | Disabled/failed module, concurrency limit, missing secret env var, process crash |
| `incompatible` | Manifest version or engine/contract range |
| `timeout` | Deadline exceeded: the module's process group is stopped |
| `cancelled` | Job cancelled, or the module disabled during the call |
| `output_invalid` | Output not JSON, over the byte limit, schema mismatch, artifact budget, credential in an artifact |
| `internal_error` | A bug; details are logged, not returned |

Every call is recorded in `modules.db` (`module status` shows recent runs).
A record has the run id, module version, surface, principal, scope, outcome,
attempts, elapsed time, catalog version, policy decision, parameters digest
and artifact ids. Inputs, outputs and credentials are never recorded.

## Jobs

A `long_running` tool returns a job at once:
`{"status": "accepted", "job": {"job_id": "job_…", "state": "running", "expires_at": …}}`.

- MCP: `module_job_status` and `module_job_cancel` (`{"job_id": ...}`).
- HTTP: `GET /modules/jobs/{id}?workspace_id=…` and `POST /modules/jobs/{id}/cancel`.
- CLI: `module job status|cancel|list`. `module invoke` waits for the jobs it
  starts; Ctrl-C cancels them.

Cancelling a job stops the module's whole process group. Disabling a module
cancels its jobs. A job id is only visible in its workspace. A finished job
is kept for `limits.job_ttl_s`.

## Artifacts and ingestion

A module may return artifacts with `text` or `base64` content. The host
validates and stores each one, content-addressed. It records the digest,
media type, scope, run, module version and parent artifacts. The artifact
id derives from the module, the record identity and the digest, so
re-fetching the same bytes gives the same id. A tool that declares
`artifact_inputs` may receive artifact ids. The host passes the module a
read-only path, and only for artifacts that belong to the call's workspace
(and project, if any).

`"ingest": true` records the artifacts in the evidence store
([CITATIONS.md](CITATIONS.md)) under an explicit destination scope, with the
run's derivation: run id, module id and version, parameters digest, and parent
revisions.

- **Source.** The module's records belong to a `module` source registered in the
  destination workspace on first use: `module:<module_id>` in the default
  workspace (`KE_KNOWLEDGE_DEFAULT_WORKSPACE`), `module:<module_id>--<workspace_id>`
  in any other.
- **Revisions.** Each artifact is a revision of the record named by its
  `record_key`, else its canonical URI, else its bytes. The same record and
  bytes are one revision, so repeating a call adds nothing; changed bytes add a
  revision. Revisions are opened, cited and exported like any other evidence.
- **Scope.** The workspace and project must exist (`unknown_scope` otherwise). A
  new record is assigned to the destination project; a record that already
  belongs to another project is not moved (`invalid_input`).
- **Lineage.** A tool that consumes artifacts from an earlier ingested run links
  to their revisions as parents, so `knowledge evidence` shows which run produced
  what from what.
- **Restarts.** Ingestion is atomic per artifact. A run interrupted midway can be
  repeated: what it already committed is replayed, not duplicated.

With `KE_KNOWLEDGE_ENABLED=0` an ingesting call answers `unavailable`; nothing
is silently dropped. Module calls without `"ingest": true` never touch the
evidence store, and the CLI opens `knowledge.db` only when a call ingests.

## Module author guide

A module is a directory with `module.json` (or `module.yaml`) plus its
program. The full JSON Schema is
`engine/src/knowledge_engine/modules/manifest.schema.json`. Here is a minimal
script module:

```json
{
  "manifest_version": 1,
  "module_id": "acme.summarize",
  "version": "1.2.0",
  "license": "Apache-2.0",
  "description": "Summarise a URL with the team's summariser.",
  "engine_compatibility": {"engine": ">=3.0.0,<4.0.0", "contract_versions": [1]},
  "transport": {"kind": "script", "command": ["python", "run.py"]},
  "capabilities": ["read", "network", "process"],
  "tools": [{
    "name": "summarize", "description": "Summarise one page.", "effect": "read", "idempotent": true,
    "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
  }],
  "configuration": {
    "settings": [{"name": "model", "type": "string", "default": "small"}],
    "secrets": [{"name": "api_key", "env": "ACME_SUMMARIZER_KEY"}]
  },
  "limits": {"deadline_ms": 30000, "concurrency": 2, "max_output_bytes": 262144},
  "health": {"command": ["python", "run.py", "--health"], "timeout_ms": 3000}
}
```

Rules:

- **Names.** `module_id` is lowercase and namespaced with `.`, `-` or `_`.
  Tool names are `[a-z][a-z0-9_]*`. `<module_id>.<tool>` must satisfy MCP's
  name rule: up to 128 characters of `A-Za-z0-9._-`. Built-in namespaces
  (`search`, `registry`, `project_context`, `board`, `knowledge`, `module(s)`
  …) are reserved.
- **Capabilities.** Every tool `effect` must be a declared capability, and
  so must the transport's own need (`process` for `script` and stdio `mcp`,
  `network` for `service` and HTTP `mcp`).
- **Secrets.** Never put secret values in a manifest. Declare
  `{"name", "env"}` references. For `service` transports, `header` and
  `header_prefix` send the value as an HTTP header.
- **Script protocol (v1).** Read one JSON request from stdin: `input`,
  `settings`, `scope`, `artifacts` (approved input artifacts with a `path`),
  `run_id` and `now`. Write one JSON object to stdout, either
  `{"output": …, "artifacts": [...]}` or
  `{"error": {"code": "invalid_input|denied|unavailable|timeout|output_invalid|internal_error", "message": "…"}}`.
  Use the request's `now` for timestamps so runs stay reproducible.
- **Artifacts.** Each is `{"media_type", "text" | "base64", "record_key"?,
  "canonical_uri"?, "title"?, "published_at"?, "retrieved_at"?, "language"?,
  "rights"?: {"license", "retention", "notes"}, "metadata"?,
  "parent_artifact_ids"?}`. Parents must be artifacts the call received.
- **Service transport.**
  `"transport": {"kind": "service", "url_setting": "endpoint"}` posts the
  same request JSON to `<endpoint><tool.path or /tool>`. The URL comes only
  from operator configuration.
- **External MCP.** For a stdio server, use
  `"transport": {"kind": "mcp", "connection": "stdio", "command": ["python", "launch.py"]}`.
  For an HTTP server, use `"connection": "streamable_http", "url_setting": "url"`.
  `remote_name` maps a tool to the upstream name. Only the tools you list
  are exposed.

### Wrapping an existing program

Wrap programs; do not copy their logic into the engine. For a CLI that
already exists, write a small adapter script that translates protocol v1 to
the program's arguments, and runs the program with `subprocess` and an argv
list (never a shell). The reference transcription module's `command` engine
shows the pattern. [MODULE-INVENTORY.md](../02-dev-docs/MODULE-INVENTORY.md)
has the step-by-step recipe.

## Reference modules

[`examples/modules/`](../examples/modules/README.md) ships two working
modules. They double as the offline test fixtures.

- `news-fetcher` (`example.news`): fetches approved inputs. These are
  `file://` fixtures under its fixture roots, or allowlisted http(s) hosts
  when `allow_live_http=true`. It returns the canonical URL, published and
  retrieved timestamps, rights and a content artifact.
- `transcription` (`example.transcribe`, long-running): takes an approved
  artifact id and returns the language, timed segments (`start_ms`/`end_ms`,
  speaker), the engine/model version and parent provenance. The fake engine
  is deterministic. Set `engine=command` and `command=...` to run an
  operator-installed transcriber.
