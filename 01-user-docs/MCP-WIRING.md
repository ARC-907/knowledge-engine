# MCP Wiring — Claude Desktop, Cursor, Continue

Knowledge-Engine ships an **MCP server** so AI assistants can search your
corpus directly. Once wired, your assistant can call `search` and get ranked
results from *your* knowledge — not a generic web answer.

This guide gets you from "installed" to "my AI assistant can search my corpus"
for the three most common MCP clients.

## What the MCP server is

`knowledge-engine mcp` runs an MCP server, built on the official MCP Python
SDK, speaking protocol `2026-07-28` over stdio; clients that still use the
`initialize` handshake get their own revision, back to `2024-11-05`. Add
`--http` to serve streamable HTTP at `http://127.0.0.1:9211/mcp` instead. It
exposes four base tools:

| Tool | What it does |
| --- | --- |
| `search` | Full-text search across enabled libraries / skills / tools. Args: `query` (required), `limit`, `kind`. |
| `registry_list` | List registry entries. Args: `kind`, `enabled_only`. |
| `registry_toggle` | Enable / disable an entry. Args: `entry_id`, `enabled`. |
| `registry_get` | Fetch one registry entry. Args: `entry_id`. |

The client launches the server as a subprocess and talks to it over stdin/
stdout. You never start it yourself — the client manages its lifecycle.

### Streamable HTTP

For clients that connect over HTTP instead of launching a subprocess, the same
server is available two ways:

- `knowledge-engine mcp --http [--host 127.0.0.1] [--port 9211]` runs it on
  its own, at `http://<host>:<port>/mcp`.
- `KE_MCP_HTTP=1 knowledge-engine serve` mounts it at `/mcp` on the dashboard
  / API server, sharing that process's registry and index.

Both apply the same `Host`-header policy (DNS-rebinding protection), chosen
from the address the server is bound to:

| Setting | Effect |
| --- | --- |
| Bound to `127.0.0.1`, `localhost` or `::1` (the defaults) | Only `Host: 127.0.0.1`, `localhost` or `[::1]` (any port) is accepted; any other name gets `421 Invalid Host header`. |
| Bound elsewhere (`0.0.0.0`, a LAN IP), `KE_MCP_ALLOWED_HOSTS` unset | The header is not checked (a reverse proxy usually sets its own `Host`). A warning is logged at startup. |
| `KE_MCP_ALLOWED_HOSTS=kb.internal,kb.example.org` | Those names (any port) are accepted in addition to the loopback names, whatever the bind address. |
| `KE_MCP_ALLOWED_HOSTS=*` | The check is turned off (logged as a warning). |

`knowledge-engine serve --host …` passes its bind address to the mount
itself. If you run uvicorn directly (`uvicorn knowledge_engine.app:create_app
--factory --host 0.0.0.0`), set `KE_BIND_HOST` to the same address, otherwise
the mount assumes a loopback bind and refuses non-loopback `Host` headers.

## Before you start: confirm the server runs

Make sure the engine is installed (see [QUICKSTART.md](QUICKSTART.md)) and
indexed (`knowledge-engine reindex`). Then sanity-check the MCP server:

```pwsh
knowledge-engine mcp --selftest
```

It builds the server, lists its tools over an in-process session and prints a
JSON summary with `"ok": true` (exit code 0). If that works, the server is
healthy and the only thing left is client config.

## The two paths you need

Every config below needs two absolute paths from *your* machine. Get them once:

1. **The Python interpreter inside the engine's virtualenv.**
   - Windows: `<repo>\engine\.venv\Scripts\python.exe`
   - macOS / Linux: `<repo>/engine/.venv/bin/python`
2. **The repo root** — the folder containing `corpus/` and `engine/`.

Replace `ABSOLUTE_PATH` in every example with your repo root. Use the full path
with no `~` and no environment variables — MCP clients do not expand them.

`scripts/mcp-client-config.example.json` in this repo is a ready-to-edit
starting point. It launches the server as `knowledge-engine mcp` (the console
script installed with the engine) and, as an alternative that needs no install,
`uvx --from "git+https://github.com/ARC-907/knowledge-engine#subdirectory=engine" knowledge-engine mcp`
(the engine is not published on PyPI); the examples below use the virtualenv's
Python so no extra tooling is needed.

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
      "command": "ABSOLUTE_PATH\\engine\\.venv\\Scripts\\python.exe",
      "args": ["-m", "knowledge_engine.mcp.server"],
      "env": {
        "KE_CORPUS_ROOT": "ABSOLUTE_PATH\\corpus",
        "KE_DATA_DIR": "ABSOLUTE_PATH\\engine\\data",
        "KE_REGISTRY_PATH": "ABSOLUTE_PATH\\corpus\\registry.json"
      }
    }
  }
}
```

**macOS / Linux:**

```json
{
  "mcpServers": {
    "knowledge-engine": {
      "command": "ABSOLUTE_PATH/engine/.venv/bin/python",
      "args": ["-m", "knowledge_engine.mcp.server"],
      "env": {
        "KE_CORPUS_ROOT": "ABSOLUTE_PATH/corpus",
        "KE_DATA_DIR": "ABSOLUTE_PATH/engine/data",
        "KE_REGISTRY_PATH": "ABSOLUTE_PATH/corpus/registry.json"
      }
    }
  }
}
```

> **Windows JSON note:** backslashes must be doubled (`\\`) inside JSON
> strings, as shown. Forward slashes also work and avoid the issue.

**Then:** fully quit Claude Desktop and reopen it. Open a chat — you should see
Knowledge-Engine listed as an available tool source (look for the tools / MCP
indicator in the chat input area). Ask something like *"search my knowledge
engine for decision frameworks"* and Claude will call the `search` tool.

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
      "command": "ABSOLUTE_PATH/engine/.venv/bin/python",
      "args": ["-m", "knowledge_engine.mcp.server"],
      "env": {
        "KE_CORPUS_ROOT": "ABSOLUTE_PATH/corpus",
        "KE_DATA_DIR": "ABSOLUTE_PATH/engine/data",
        "KE_REGISTRY_PATH": "ABSOLUTE_PATH/corpus/registry.json"
      }
    }
  }
}
```

(On Windows use `engine\\.venv\\Scripts\\python.exe` for `command` and doubled
backslashes in the paths, as in the Claude Desktop section.)

**Then:** open Cursor Settings → MCP. Knowledge-Engine should appear with a
green status dot and its four tools listed. In the agent chat, the model can
now call `search`.

---

## Continue

Continue (the VS Code / JetBrains extension) reads MCP servers from its config.

**Config file location:** `~/.continue/config.yaml`
(older installs use `~/.continue/config.json`).

**`config.yaml`:**

```yaml
mcpServers:
  - name: knowledge-engine
    command: ABSOLUTE_PATH/engine/.venv/bin/python
    args:
      - "-m"
      - "knowledge_engine.mcp.server"
    env:
      KE_CORPUS_ROOT: ABSOLUTE_PATH/corpus
      KE_DATA_DIR: ABSOLUTE_PATH/engine/data
      KE_REGISTRY_PATH: ABSOLUTE_PATH/corpus/registry.json
```

**`config.json` (older format):**

```json
{
  "mcpServers": [
    {
      "name": "knowledge-engine",
      "command": "ABSOLUTE_PATH/engine/.venv/bin/python",
      "args": ["-m", "knowledge_engine.mcp.server"],
      "env": {
        "KE_CORPUS_ROOT": "ABSOLUTE_PATH/corpus",
        "KE_DATA_DIR": "ABSOLUTE_PATH/engine/data",
        "KE_REGISTRY_PATH": "ABSOLUTE_PATH/corpus/registry.json"
      }
    }
  ]
}
```

**Then:** reload the Continue extension. The `search` tool becomes available to
the agent.

---

## Any other MCP client

The pattern is identical everywhere. The server is launched as:

```
<venv-python>  -m  knowledge_engine.mcp.server
```

(or, equivalently, `knowledge-engine mcp`, or without installing:
`uvx --from "git+https://github.com/ARC-907/knowledge-engine#subdirectory=engine" knowledge-engine mcp`)

with these environment variables set:

| Env var | Value |
| --- | --- |
| `KE_CORPUS_ROOT` | `<repo>/corpus` |
| `KE_DATA_DIR` | `<repo>/engine/data` |
| `KE_REGISTRY_PATH` | `<repo>/corpus/registry.json` |

Any MCP client that can launch a stdio server with a command, args, and an env
block can run Knowledge-Engine. Translate the JSON/YAML above into your client's
config format.

## Verifying end to end

Once configured, the surest test is to ask your assistant a question whose
answer is only in your corpus, and watch it call `search`. With the shipped
demo corpus, *"use the knowledge engine to find the demo applied example"*
should return a hit from the demo library.

You can also exercise the server from a terminal without any client:

```pwsh
knowledge-engine mcp --selftest
```

It prints a JSON summary (`"ok": true`, the negotiated protocol version and
the tool names) and exits 0. If that works but the client doesn't, the problem
is in the client config — almost always a wrong absolute path.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Server not listed in the client | Config file in the wrong location, or invalid JSON/YAML. Validate the file. |
| Server listed but red / failed | Wrong `command` path — point it at the venv's `python`, not system Python. |
| Server starts but `search` returns nothing | The corpus isn't indexed. Run `knowledge-engine reindex`. |
| Works in terminal, fails in client | The client can't find the interpreter or corpus. Re-check every absolute path; no `~`, no env vars. |
| Windows: "file not found" on the command | Backslashes not doubled in JSON, or path has spaces — quote it / double the slashes. |
| Changes to the corpus don't show up | The MCP server reads the index at each call, but you must `reindex` after changing content. |
| HTTP: `/mcp` answers `421 Invalid Host header` | The server thinks it is bound to loopback but clients use another name. Pass the real bind address (`serve --host …`, or `KE_BIND_HOST` with plain uvicorn) or list the names in `KE_MCP_ALLOWED_HOSTS` — see [Streamable HTTP](#streamable-http). |
