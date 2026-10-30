"""Scoped knowledge: versioned contracts, scope semantics and source-owned metadata (V2 P0/P1).

Modules (importing this package does no I/O and loads none of them):

* :mod:`.contracts` -- Pydantic models of the public contract (schema version 1):
  ``QueryContext``, ``SearchHit``, ``SearchEnvelope``, ``SourceStatus``,
  ``OpenResult``, the typed locator union, the controlled vocabularies and the
  record schemas (``SourceRecord``, ``Revision``, ``EvidenceSpan``,
  ``Derivation``, ``Citation``) plus identity helpers;
* :mod:`.errors` -- the typed error hierarchy with stable codes;
* :mod:`.adapter` -- the ``SourceAdapter`` protocol and its capability descriptor;
* :mod:`.scope` -- scope resolution and the reference filter predicate (ADR 0006);
* :mod:`.store` -- the ``knowledge.db`` repository (workspaces, projects,
  sources, collections, membership, classification, profiles, checkpoints);
* :mod:`.backfill` -- ``knowledge migrate`` (dry run, backup, resumable apply);
* :mod:`.service` -- the admin operation catalog shared by HTTP, MCP and CLI;
* :mod:`.mcp_tools` -- the ``knowledge_*`` MCP tool group;
* :mod:`.config` -- the resolved configuration (``KE_KNOWLEDGE_DB`` and friends);
* :mod:`.broker` -- the retrieval broker (scoped search/open over source adapters, V2 P2);
* :mod:`.sources` -- the corpus and project-context adapters and the default factory;
* :mod:`.broker_api` -- request models shared by the broker's HTTP, MCP and CLI bindings;
* :mod:`.broker_mcp` -- the ``knowledge.search`` / ``knowledge.open`` MCP tools.

The legacy ``/search`` route, MCP ``search`` tool and CLI ``search`` command
do not read this package.
"""
