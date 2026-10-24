# ADR 0001 — Build the MCP server on the official MCP Python SDK

- **Status:** Accepted (3.0.0)
- **Date:** 2026-10

## Context

Releases 1.x and 2.x shipped a hand-written JSON-RPC 2.0 stdio server
(`knowledge_engine.mcp.server`, protocol `2024-11-05`). It had no transport
other than stdio, returned tool failures as JSON-RPC `-32602` errors (which
some clients treat as fatal), and every protocol revision had to be
re-implemented by hand. The project-context (46 tools) and agent-board (15
tools) groups registered themselves through an in-house tool-group discovery
mechanism with explicit JSON input schemas.

## Decision

Rebuild the server on the official MCP Python SDK (`mcp>=2.3,<3`):

- Base tools are plain typed functions; the SDK generates their input schemas.
- The existing tool groups are mounted through an adapter
  (`knowledge_engine.mcp.toolkit.register_group`) that keeps their original
  names, descriptions and input schemas and validates arguments against the
  advertised schema, so clients see the same tools as before.
- Transports: stdio (`knowledge-engine mcp`), a standalone streamable-HTTP
  listener (`--http`), and a mount at `/mcp` on the FastAPI app
  (`KE_MCP_HTTP=1`) sharing its registry and index. The HTTP transports apply
  the SDK's DNS-rebinding protection, configurable with `KE_MCP_ALLOWED_HOSTS`.
- Tool failures are returned as tool results with `isError: true`.
- `knowledge-engine mcp --selftest` exercises the server in-process as a
  packaging smoke test; a frozen snapshot of the pre-migration tool schemas
  (`engine/tests/snapshots/mcp_tools.json`) guards the wire format.
- `knowledge_engine.mcp_server` stays as a deprecated shim for one major.

## Consequences

- The default protocol revision follows the SDK (`2026-07-28`); `initialize`
  clients still negotiate back to `2024-11-05`. `ping` is gone with that
  revision.
- `TOOLS`, `Server` and `PROTOCOL_VERSION` are no longer exported from
  `knowledge_engine.mcp.server`; embedders use `build_server()`.
- Base-tool schemas carry pydantic `title`s and `anyOf[..., null]` for
  optional parameters; names, parameters, enums and defaults are unchanged.
- The adapter relies on SDK internals for explicit input schemas and must be
  revisited when the SDK offers a public API for that; the dependency is
  pinned to the 2.x major.
- The engine gains the `mcp`, `anyio` and `starlette` runtime dependencies
  (the latter two were already transitive).
