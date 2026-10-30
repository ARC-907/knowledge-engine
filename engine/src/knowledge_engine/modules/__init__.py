"""Modules: executable utilities behind one manifest, catalog and dispatcher (V2 P5/P6).

Importing this package does no I/O and loads none of its modules:

* :mod:`.manifest` -- manifest version 1, validated as data (never executed);
* :mod:`.catalog` -- ``modules.db``: intent/configuration, observed health,
  sanitized runs, jobs and artifact records;
* :mod:`.dispatcher` -- :class:`~knowledge_engine.modules.dispatcher.ModuleHost`,
  the lifecycle operations and the one invocation path CLI, HTTP and MCP share;
* :mod:`.transports` -- script (the tool host's bounded runner), service and
  external MCP transports;
* :mod:`.artifacts` -- artifact normalization and the content-addressed byte store;
* :mod:`.ingestion` -- the evidence-ingestion interface (P3 implements it)
  and an in-memory test double;
* :mod:`.mcp_tools` -- the MCP group that advertises enabled modules' tools;
* :mod:`.config` -- the resolved configuration (``KE_MODULES_ENABLED``, ``KE_MODULES_DIR``, ``KE_MODULES_DB``, ``KE_MODULES_HOST_CAPABILITIES``).

Operator and author guide: ``01-user-docs/MODULES.md``.
"""
