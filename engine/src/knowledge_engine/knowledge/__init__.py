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
* :mod:`.config` -- the resolved configuration (``KE_KNOWLEDGE_DB`` and friends).

The legacy ``/search`` route, MCP ``search`` tool and CLI ``search`` command
do not read this package.
"""
