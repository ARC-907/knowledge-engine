"""Evidence: immutable revisions, typed locators, provenance, resolution and citations (V2 P3/P4).

Modules (importing this package does no I/O and loads none of them):

* :mod:`.policy` -- the operator retention/deletion policy
  (``KE_EVIDENCE_ON_DELETE``, ``KE_EVIDENCE_RETENTION``, ``KE_EVIDENCE_MAX_BYTES``);
* :mod:`.extract` -- extraction of typed locators (Markdown headings, plain text,
  transcript segments, code symbols; PDF is reported ``unsupported``) and the
  slicing of a locator out of exact revision bytes;
* :mod:`.store` -- the evidence repository in ``knowledge.db``: source records,
  revisions, retained bytes, aliases, evidence spans, derivations, tombstones,
  idempotent ingestion and the cross-store outbox;
* :mod:`.resolve` -- ``open``: scope recheck, exact-revision load, digest and
  span verification, returning the P0 ``OpenResult``;
* :mod:`.citations` -- claim-to-evidence validation, Markdown and CSL-JSON export;
* :mod:`.snapshot` -- ``knowledge snapshot``: record the corpus files as revisions.

The retrieval broker (P2) calls :func:`.resolve.open_evidence` for its open path.
"""
