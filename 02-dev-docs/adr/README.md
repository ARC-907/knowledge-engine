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
