# Module inventory and wrapping recipe

V2-DESIGN ("Reference integrations") asks to inventory the owner's existing
media/news and transcription programs and wrap them, not reimplement them.

## Inventory status

**The owner's actual utilities were not available to this implementation**
(V2 P5/P6, issue #37). No source code, CLI help, API description, license,
runtime versions, credentials or file formats were provided. So:

- No interface was invented for them. The two modules in
  [`examples/modules/`](../examples/modules/README.md) are *reference*
  modules with synthetic offline fixtures. They demonstrate the contract
  (manifest, protocol, artifacts, jobs, provenance), not the owner's tools.
- Wrapping the real programs is an implementation dependency of the
  remaining P6 work. Record the inventory below for each program before
  wrapping it.

| Item to record | Why |
| --- | --- |
| Interface: CLI (argv, stdin/stdout, exit codes) or HTTP API (paths, methods, schemas) | Choose `script` / `service` / `mcp` transport and write the adapter |
| License and redistribution terms | Manifest `license`; whether the module may ship in this repository |
| Runtime and versions (interpreter, binaries, models) | Engine/host prerequisites; `health` probe |
| Credentials | Secret *references* (`env` names), never values |
| Input/output formats, sizes | Input/output JSON Schemas, `limits`, artifact media types |
| Operational limits (duration, concurrency, rate limits) | `deadline_ms`, `concurrency`, `long_running`, `max_retries`, `idempotent` |
| Deployment (same host, container, remote service) | Transport and isolation (modules are not sandboxed) |

## Wrapping recipe

### An existing CLI (script transport)

1. Create `<KE_MODULES_DIR>/<name>/`. Do not put it inside the corpus.
2. Write `adapter.py`. It reads the protocol v1 request from stdin, builds
   the program's argv from `input` and `settings`, runs it with
   `subprocess.run(argv, capture_output=True, check=False)` (never
   `shell=True`), and maps the result. Use `{"output": ...}` plus optional
   `artifacts` on success, and `{"error": {"code", "message"}}` on failure
   (`unavailable` for a non-zero exit, `invalid_input` for a rejected
   input). Keep business logic in the program.
3. Write `module.json`:

   ```json
   {
     "manifest_version": 1,
     "module_id": "owner.transcriber",
     "version": "<program version>",
     "license": "<program license>",
     "description": "Wraps <program> <version>.",
     "engine_compatibility": {"engine": ">=3.0.0,<4.0.0", "contract_versions": [1]},
     "transport": {"kind": "script", "command": ["python", "adapter.py"]},
     "capabilities": ["read", "process"],
     "tools": [{"name": "transcribe", "description": "…", "effect": "read",
                "idempotent": true, "long_running": true, "artifact_inputs": ["artifact_id"],
                "input_schema": {"type": "object", "properties": {"artifact_id": {"type": "string"}},
                                 "required": ["artifact_id"]},
                "output_schema": {"type": "object"}}],
     "configuration": {
       "settings": [{"name": "binary", "type": "string", "required": true,
                     "description": "Absolute path of the installed program"}],
       "secrets": [{"name": "license_key", "env": "OWNER_TRANSCRIBER_KEY", "required": false}]
     },
     "limits": {"deadline_ms": 1800000, "concurrency": 1, "max_artifact_bytes": 52428800},
     "health": {"command": ["python", "adapter.py", "--health"]}
   }
   ```

4. Make the program available to the adapter. You can configure its path
   (`module configure owner.transcriber --set binary=/opt/…`), or use the
   reference transcription module's `engine=command` + `command=[…]`
   setting, which substitutes `{input}`. The program runs in the module's
   process group, so cancellation and timeouts stop it.
5. Run `module register`, then `module configure`, then
   `module enable --grant read --grant process`, then
   `module status --probe`, then a test `module invoke`. Restart the MCP
   server to advertise the tool.

### An existing HTTP API (service transport)

Declare a URL setting and point the transport at it:

```json
"transport": {"kind": "service", "url_setting": "endpoint"},
"capabilities": ["read", "network"],
"configuration": {
  "settings": [{"name": "endpoint", "type": "string", "required": true}],
  "secrets": [{"name": "token", "env": "OWNER_NEWS_TOKEN", "header": "Authorization", "header_prefix": "Bearer "}]
},
"health": {"path": "/health"}
```

The engine POSTs the protocol v1 request JSON to `<endpoint><tool.path>`.
If the API does not speak that shape, put a thin adapter service (or a
script adapter using `urllib`) in front of it. Grant `network`; the host
policy must allow it.

### An existing MCP server (mcp transport)

Use `"transport": {"kind": "mcp", "connection": "stdio", "command": ["python", "launch.py"]}`,
where `launch.py` starts the server. The argv rule requires an allowlisted
interpreter and a script, so wrap binaries in a launcher. For a server
reachable over HTTP, use `"connection": "streamable_http", "url_setting": "url"`.
List only the tools to expose and map them with `remote_name`. Never forward
generic shell/edit tools to implement a read-only facade (V2-DESIGN,
"Serena integration").
