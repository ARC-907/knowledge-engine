# ADR 0011 — Retrieval broker: candidate caps, fusion, filtering and source mapping

- **Status:** Accepted (V2 P2, issue #35)
- **Date:** 2026-10

## Context

The V2 design ("Query and adapter contracts") fixes the broker sequence but
leaves several choices to the implementation: how many candidates each source
contributes, how rankings of different stores are combined, how scope filters
reach indexes that live outside `knowledge.db`, what a revision id means before
revision storage (P3) exists, and how project-context stores (identified by
fingerprints) map to scoped-knowledge projects.

## Decision

- **One service, three bindings.** `knowledge_engine.knowledge.broker.KnowledgeBroker`
  serves HTTP (`/knowledge/search`, `/knowledge/open`), MCP (`knowledge.search`,
  `knowledge.open`) and CLI (`knowledge search|open`) through the request models
  of `knowledge_engine.knowledge.broker_api`.
- **Snapshot.** Each request reads one consistent snapshot from `knowledge.db`
  (`KnowledgeStore.snapshot`): the effective scope, the workspace's sources
  with their configuration, and the metadata of every in-scope record (at most
  1 000 000). Adapters are bound to that snapshot per request, so the P0
  `SourceAdapter` signature stays unchanged.
- **Filters before limits.** The corpus FTS side reads matches in BM25 order
  and keeps in-scope paths until `limit` are found
  (`Indexer.search_where`, at most 50 000 ranked matches examined, reported as a
  warning when reached). This is exact for every query whose in-scope matches
  are within the cap; pushing the record set into the FTS query (temporary
  table or `ATTACH`) was rejected because the index and `knowledge.db` are
  separate stores with separate connections and locks. The vector side filters
  per file before taking the top files (exact).
- **Candidates and fusion.** Each source contributes at most `limit` hits in
  its own order. The broker fuses the per-source rankings with reciprocal rank
  fusion (k = 60), never comparing raw BM25 and cosine scores, and breaks ties
  on `(source_id, record_id, revision_id)`. Because fusion preserves each
  source's order, the fused top `limit` equals the fused top `limit` of the
  complete source rankings; the only approximation is the corpus `hybrid` mode,
  which keeps the legacy bounded FTS/vector candidates.
- **Visibility recheck and dedupe.** Hits of record-backed sources must be in
  the snapshot's in-scope set (their collections and research type are taken
  from the snapshot); other sources are checked with `record_in_scope`.
  Repeated hits of one identity are merged (collection union).
- **Failures.** Sources run on a bounded pool (4 workers per broker) under the
  request deadline; a late source is `timeout` and its result is ignored (Python
  threads cannot be killed; adapters open and close their own connections per
  call). A source that cannot serve the requested mode is `unavailable`; when no
  selected source can answer, the request is `unavailable` — including a
  `vector` request without an index.
- **Revision ids before revision storage.** A hit's `revision_id` is the
  content-addressed id (ADR 0007) of the record's bytes at search time. `open`
  serves that revision when the bytes still match (`verified`), otherwise
  `unsupported` (older revisions are not stored) or `unavailable` (bytes gone);
  it never substitutes the latest content.
- **Project context.** An operator links a project to a fingerprint at a root
  (`project_context_link`); links are the configuration of the workspace's
  source `project_context:<workspace_id>`, so no `knowledge.db` migration is
  needed. Only linked fingerprints are searchable, each with its fingerprint as
  an SQL filter (projects sharing a name share one content DB). Documents are
  records of their linked project, research type `other`, without tags or
  collections. Record ids are `<project_id>:<document id>`.
- **Board.** The agent board is not a broker source by default
  (`BOARD_SOURCE_AVAILABLE = False`); a `board` source is reported unavailable.

## Consequences

- Broker results are deterministic for a fixed snapshot and index state.
- P3 can replace the search-time revision ids with stored revisions without a
  contract change: the formula is the same.
- A scope with more than 1 000 000 records must be narrowed; a corpus filter
  whose matches lie beyond 50 000 ranked rows can return fewer than `limit`
  hits (with a warning).
- Changing k, the tie-break or the per-source cap changes result order and is a
  documented behaviour change.
