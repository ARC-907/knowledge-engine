# MCP Wiring — Claude Desktop, Cursor, Continue, any MCP client

Knowledge-Engine ships an **MCP server** so AI assistants can search your
corpus directly. Once wired, your assistant can call `search` and get ranked
results from *your* knowledge.

This guide gets you from "installed" to "my assistant can search my corpus"
for the three most common MCP clients, plus the generic recipe.

## What the MCP server is

`knowledge-engine mcp` runs an MCP server built on the official MCP Python SDK.
It speaks protocol `2026-07-28` over stdio; clients that use the `initialize`
handshake negotiate their own revision, back to `2024-11-05`. Add `--http` to
serve streamable HTTP at `http://127.0.0.1:9211/mcp` instead. The four base
tools:

| Tool | What it does |
| --- | --- |
| `search` | Search enabled libraries / skills / kits / tools. Args: `query` (required), `limit` (≥ 1, default 10), `kind`, `mode` (`fts` default; `vector` / `hybrid` need the [embedding index](EMBEDDINGS.md)). Omit `kind` for all kinds (an empty string is rejected). |
| `registry_list` | List registry entries. Args: `kind`, `enabled_only`. |
| `registry_toggle` | Enable / disable an entry. Args: `entry_id`, `enabled`. |
| `registry_get` | Fetch one registry entry. Args: `entry_id`. |

Two optional tool groups are mounted when their packages import and are
enabled in config: the project-context group (`project_context.*`, 46 tools)
and the agent-board group (`board_*`, 15 tools; see
[`02-dev-docs/AGENT-BOARD.md`](../02-dev-docs/AGENT-BOARD.md)). A default
install lists 65 tools. A tool that fails returns a tool result with
`isError: true`; it never takes the server down.

The client launches the server as a subprocess and talks to it over stdin /
stdout. You never start it yourself — the client manages its lifecycle. The
server logs to stderr at `KE_LOG_LEVEL` (default INFO); stdout belongs to the
protocol.

### Streamable HTTP

For clients that connect over HTTP instead of launching a subprocess, the same
server is available two ways:

- `knowledge-engine mcp --http [--host 127.0.0.1] [--port 9211]` runs it on
  its own, at `http://<host>:<port>/mcp`.
- `KE_MCP_HTTP=1 knowledge-engine serve` mounts it at `/mcp` on the dashboard /
  API server, sharing that process's registry and index.

Both apply the same `Host`-header policy (DNS-rebinding protection), chosen
from the address the server is bound to:

| Setting | Effect |
| --- | --- |
| Bound to `127.0.0.1`, `localhost` or `::1` (the defaults) | Only `Host: 127.0.0.1`, `localhost` or `[::1]` (any port) is accepted; any other name gets `421 Invalid Host header`. |
| Bound elsewhere (`0.0.0.0`, a LAN IP), `KE_MCP_ALLOWED_HOSTS` unset | The header is not checked (a reverse proxy usually sets its own `Host`). A warning is logged at startup, and `--http` on a non-loopback host also warns that the endpoint has no authentication. |
| `KE_MCP_ALLOWED_HOSTS=kb.internal,kb.example.org` | Those names (any port) are accepted in addition to the loopback names, whatever the bind address. |
| `KE_MCP_ALLOWED_HOSTS=*` | The check is turned off (logged as a warning). |

`knowledge-engine serve --host …` passes its bind address to the mount itself.
If you run uvicorn directly (`uvicorn knowledge_engine.app:create_app --factory
--host 0.0.0.0`), set `KE_BIND_HOST` to the same address, otherwise the mount
assumes a loopback bind and refuses non-loopback `Host` headers. The HTTP
endpoint carries no authentication of its own — put it behind the same reverse
proxy as the rest of the API ([DEPLOYMENT.md](DEPLOYMENT.md)).

## Before you start: confirm the server runs

Make sure the engine is installed (see [QUICKSTART.md](QUICKSTART.md)) and
indexed (`knowledge-engine reindex`). Then sanity-check the MCP server:

```
knowledge-engine mcp --selftest
```

It builds the server, lists its tools over an in-process session and prints a
JSON summary with `"ok": true`, the protocol version and the tool names (exit
code 0). If that works, the server is healthy and the only thing left is
client config.

## Three ways to launch it

Every client config needs a command to run and, usually, one environment
variable telling the engine where the corpus is. Pick one launcher:

| Launcher | `command` | Needs |
| --- | --- | --- |
| **Checkout venv** (recommended) | `ABSOLUTE_PATH/engine/.venv/bin/knowledge-engine` (Windows: `ABSOLUTE_PATH\\engine\\.venv\\Scripts\\knowledge-engine.exe`) | `uv sync --project engine` has been run |
| **Global tool** | `knowledge-engine` | `uv tool install "git+https://github.com/ARC-907/knowledge-engine@v3.0.0#subdirectory=engine"` |
| **No install** | `uvx` with args `--from git+https://github.com/ARC-907/knowledge-engine@v3.0.0#subdirectory=engine knowledge-engine mcp` | `uv` on PATH (first start downloads and builds the package) |

In every case the args are `["mcp"]` (the `uvx` form puts `mcp` last), and the
engine locates the corpus through **`KE_HOME`**: the directory that contains
`corpus/registry.json`. For a checkout that is the repository root. The index
and other runtime data go to `<KE_HOME>/.knowledge-engine/data`. If your corpus
lives elsewhere, set `KE_CORPUS_ROOT`, `KE_REGISTRY_PATH` and `KE_DATA_DIR`
individually instead ([CONFIGURATION.md](CONFIGURATION.md)).

Replace `ABSOLUTE_PATH` in every example with the real path. Use the full path
with no `~` and no environment variables — MCP clients do not expand them.
`scripts/mcp-client-config.example.json` in this repository is a ready-to-edit
starting point with the checkout and `uvx` forms.

---

## Claude Desktop

**Config file location:**

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

If the file doesn't exist, create it. Add a `knowledge-engine` entry under
`mcpServers`:

**Windows:**

```json
{
  "mcpServers": {
    "knowledge-engine": {
      "command": "ABSOLUTE_PATH\\engine\\.venv\\Scripts\\knowledge-engine.exe",
      "args": ["mcp"],
      "env": { "KE_HOME": "ABSOLUTE_PATH" }
    }
  }
}
```

**macOS / Linux:**

```json
{
  "mcpServers": {
    "knowledge-engine": {
      "command": "ABSOLUTE_PATH/engine/.venv/bin/knowledge-engine",
      "args": ["mcp"],
      "env": { "KE_HOME": "ABSOLUTE_PATH" }
    }
  }
}
```

> **Windows JSON note:** backslashes must be doubled (`\\`) inside JSON
> strings, as shown. Forward slashes also work and avoid the issue.

**Then:** fully quit Claude Desktop and reopen it. Open a chat — Knowledge-Engine
appears as an available tool source (look for the tools / MCP indicator in the
chat input area). Ask something like *"search my knowledge engine for the demo
applied example"* and the assistant calls the `search` tool.

---

## Cursor

Cursor reads MCP servers from a `mcp.json` file.

**Config file location:**

- Project-scoped: `.cursor/mcp.json` in your project root.
- Global: `~/.cursor/mcp.json`.

```json
{
  "mcpServers": {
    "knowledge-engine": {
      "command": "ABSOLUTE_PATH/engine/.venv/bin/knowledge-engine",
      "args": ["mcp"],
      "env": { "KE_HOME": "ABSOLUTE_PATH" }
    }
  }
}
```

(On Windows use `engine\\.venv\\Scripts\\knowledge-engine.exe` for `command`
and doubled backslashes in the paths, as in the Claude Desktop section.)

**Then:** open Cursor Settings → MCP. Knowledge-Engine should appear with a
green status dot and its tools listed. In the agent chat, the model can now
call `search`.

---

## Continue

Continue (the VS Code / JetBrains extension) reads MCP servers from its config.

**Config file location:** `~/.continue/config.yaml`
(older installs use `~/.continue/config.json`).

**`config.yaml`:**

```yaml
mcpServers:
  - name: knowledge-engine
    command: ABSOLUTE_PATH/engine/.venv/bin/knowledge-engine
    args:
      - mcp
    env:
      KE_HOME: ABSOLUTE_PATH
```

**`config.json` (older format):**

```json
{
  "mcpServers": [
    {
      "name": "knowledge-engine",
      "command": "ABSOLUTE_PATH/engine/.venv/bin/knowledge-engine",
      "args": ["mcp"],
      "env": { "KE_HOME": "ABSOLUTE_PATH" }
    }
  ]
}
```

**Then:** reload the Continue extension. The `search` tool becomes available to
the agent.

---

## Any other MCP client

The pattern is identical everywhere. The server is launched as

```
knowledge-engine mcp
```

(the console script from the checkout venv, a `uv tool install`, or
`uvx --from "git+https://github.com/ARC-907/knowledge-engine@v3.0.0#subdirectory=engine" knowledge-engine mcp`;
`python -m knowledge_engine.mcp.server` with the venv's interpreter is
equivalent) with this environment:

| Env var | Value |
| --- | --- |
| `KE_HOME` | the directory containing `corpus/registry.json` (the repository root for a checkout) |

or, for a corpus outside that layout, `KE_CORPUS_ROOT`, `KE_REGISTRY_PATH` and
`KE_DATA_DIR` set individually. Any MCP client that can launch a stdio server
with a command, args and an env block can run Knowledge-Engine; translate the
JSON / YAML above into your client's format. Clients that speak streamable
HTTP connect to `http://127.0.0.1:9211/mcp` after `knowledge-engine mcp --http`
(or to `/mcp` on the API server with `KE_MCP_HTTP=1`).

## Verifying end to end

Once configured, the surest test is to ask your assistant a question whose
answer is only in your corpus, and watch it call `search`. With the shipped
demo corpus, *"use the knowledge engine to find the demo applied example"*
should return a hit from the demo library.

You can also exercise the server from a terminal without any client:

```
knowledge-engine mcp --selftest
```

If that works but the client doesn't, the problem is in the client config —
almost always a wrong absolute path.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Server not listed in the client | Config file in the wrong location, or invalid JSON/YAML. Validate the file. |
| Server listed but red / failed | Wrong `command` path — point it at the venv's `knowledge-engine` (or `python`), not a system one. Run the exact command in a terminal; the error is on stderr. |
| Server starts, then exits: registry invalid | `registry.json` fails schema validation. Run `knowledge-engine registry validate` and fix what it reports. |
| Server starts but `search` returns nothing | The corpus isn't indexed, or `KE_HOME` points at the wrong directory. Run `knowledge-engine reindex` with the same environment. |
| Works in terminal, fails in client | The client can't find the interpreter or corpus. Re-check every absolute path; no `~`, no env vars. |
| Windows: "file not found" on the command | Backslashes not doubled in JSON, or path has spaces — quote it / double the slashes. |
| Changes to the corpus don't show up | The MCP server reads the index at each call, but you must `reindex` after changing content (and `embeddings build --incremental` for vector / hybrid). |
| `kind` filter rejected | `kind` must be `library`, `skill`, `kit` or `tool`; omit it (or pass null) for all kinds. |
| HTTP: `/mcp` answers `421 Invalid Host header` | The server thinks it is bound to loopback but clients use another name. Pass the real bind address (`serve --host …`, or `KE_BIND_HOST` with plain uvicorn) or list the names in `KE_MCP_ALLOWED_HOSTS` — see [Streamable HTTP](#streamable-http). |
