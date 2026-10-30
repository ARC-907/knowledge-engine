# Architecture decision records

Short records (MADR style: context, decision, consequences) of decisions with
lasting effect on the engine. Add a new file `NNNN-short-title.md` for each
decision; never rewrite history — supersede a record with a new one and link
both ways.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-mcp-sdk-v2.md) | Build the MCP server on the official MCP Python SDK | Accepted |
| [0002](0002-default-paths.md) | Default paths from `KE_HOME`, runtime data under `.knowledge-engine/data` | Accepted |
| [0003](0003-board-auth-model.md) | Agent board authentication model | Accepted |
| [0004](0004-merge-train.md) | Deliver large changes as a merge train of stacked PRs with a local gate | Accepted |
| [0005](0005-nested-entry-ownership.md) | Nested registry entries: the deepest enabled entry owns a file | Accepted |
| [0006](0006-scope-defaults-and-filter-semantics.md) | Default legacy/workspace inclusion and scope filter semantics | Accepted |
| [0007](0007-record-revision-identity-and-locators.md) | Canonical record/revision identity and locator units | Accepted |
| [0008](0008-retention-and-tombstones.md) | Retention and tombstone policy | Accepted |
| [0009](0009-module-catalog-persistence.md) | Module catalog persistence and restart behaviour | Accepted |
| [0010](0010-serena-mapping-deferred.md) | Exact Serena compatibility mapping deferred to the Serena slice | Accepted (deferral) |
| [0011](0011-retrieval-broker-fusion-and-adapters.md) | Retrieval broker: candidate caps, fusion, filtering and source mapping | Accepted |
| [0012](0012-evidence-store-and-ingestion.md) | Evidence store layout, idempotent ingestion and rename detection | Accepted |
| [0013](0013-citation-identity-and-export.md) | Citation identity, claim validation and export formats | Accepted |
