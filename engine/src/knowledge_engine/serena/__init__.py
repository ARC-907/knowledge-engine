"""Optional, read-only Serena adapter (V2 P7; ADR 0016).

`Serena <https://github.com/oraios/serena>`_ is an external MCP server that
answers symbol questions through a language server. It is licensed
GPL-3.0-or-later and is never bundled: operators install it separately and
point ``KE_SERENA_COMMAND`` at it. The engine talks to it only over stdio MCP
and only through three read-only tools.

Modules:

* :mod:`.config` -- settings (``KE_SERENA_ENABLED`` ...) and the operator project file
  (project id, repository root, repository identity);
* :mod:`.witness` -- the pinned compatibility record: server name/version and
  the hash of every mapped tool's input schema;
* :mod:`.session` -- one Serena process per project (stdio MCP, isolated
  ``HOME``, forced tool interface, usage reporting off), serialized calls,
  timeouts, restart with backoff;
* :mod:`.manager` -- the sessions of all configured projects, the session cap
  and idle shutdown;
* :mod:`.repo` -- repository state (commit, or a snapshot digest when the
  working tree is dirty) and contained file reads;
* :mod:`.tools` -- the read-only allowlist, the engine-side argument schemas
  and the normalization of results into code locators;
* :mod:`.adapter` -- the broker source ``serena:<project_id>``;
* :mod:`.mcp_tools` -- the ``serena.*`` MCP tools.

Nothing here runs unless ``KE_SERENA_ENABLED=1``; corpus search never
depends on it.
"""
