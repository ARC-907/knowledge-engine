# Scoped knowledge search and open

The retrieval broker searches the sources of one workspace — the corpus
(`corpus:base`) and linked project-context stores — inside an explicit scope,
and opens a hit at its exact revision. It uses the workspaces, projects,
collections and classification described in
[SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md); run `knowledge migrate --apply`
first.

The legacy `/search` route, the MCP `search` tool and `knowledge-engine
search` are unchanged and do not use the broker. The dashboard's Knowledge
tab is a browser front end to these routes ([DASHBOARD.md](DASHBOARD.md)).

| Surface | Search | Open |
| --- | --- | --- |
| HTTP | `POST /knowledge/search` | `POST /knowledge/open`, `GET /knowledge/open?workspace_id=…&source_id=…&record_id=…[&revision_id=…]` |
| MCP | `knowledge.search` | `knowledge.open` |
| CLI | `knowledge-engine knowledge search QUERY [flags]` | `knowledge-engine knowledge open SOURCE_ID RECORD_ID [--revision REV] [flags]` |

All three return the same JSON. The schemas are frozen in
`engine/tests/snapshots/openapi.json` (`SearchRequest`,
`KnowledgeSearchEnvelope`, `OpenRequest`, `OpenResult`) and
`engine/tests/snapshots/mcp_tools.json`.

## Searching

A request is a query plus a query context:

```json
{
  "query": "spring tides",
  "context": {
    "workspace_id": "default",
    "project_id": "harbour",
    "include_workspace": true,
    "limit": 5
  }
}
```

```bash
knowledge-engine knowledge search "spring tides" --project harbour --include-workspace --limit 5
```

| Context field | Default | Meaning |
| --- | --- | --- |
| `workspace_id` | required (CLI: the configured default workspace) | The knowledge boundary |
| `project_id` | none | Only that project's records |
| `include_workspace` | `false` | With `project_id`: also records that belong to no project |
| `profile_id` | `KE_KNOWLEDGE_PROFILE` | Fills every selection you omit |
| `source_ids`, `collection_ids`, `research_types` | from the profile | Values ORed; an explicit `[]` selects nothing |
| `tags` | from the profile | ANDed; `[]` means no tag constraint |
| `mode` | `fts` | `fts`, `vector` or `hybrid` |
| `limit` | `10` (1–100) | Hits returned |
| `timeout_ms` | `10000` (1–120000) | Deadline for every source |
| `byte_budget` | `1048576` | Bytes of titles plus excerpts in the response |

The scope rules are the same as for `record list` (ADR 0006). An unknown
workspace, project, source, collection or profile is an `unknown_scope` error,
never an empty or global result. A `principal` in the request is rejected.

Filters apply **before** the limit. A query filtered to a project, collection,
type or tag still returns up to `limit` matching hits, not the matching part of
an unfiltered top `limit`.

### The response

```json
{
  "schema_version": 1,
  "contract_version": 1,
  "hits": [
    {
      "schema_version": 1,
      "source_id": "corpus:base",
      "record_id": "file-796a77177822595e081f023469eef1d3",
      "revision_id": "rev_176a0a5ca0ab8e8ec174242a0c8d8d8e",
      "scope": {"workspace_id": "default", "project_id": "harbour"},
      "collection_ids": ["reading"],
      "research_type": "note",
      "title": "Tides",
      "locator": null,
      "excerpt": "# <mark>Tides</mark>\n\n<mark>Spring</mark> <mark>tides</mark> follow the new and full moon.\n",
      "provenance_id": "entry:notes",
      "ranking": {
        "native_rank": 1,
        "native_score": -2.2796532381438045e-06,
        "score_kind": "bm25",
        "fused_rank": 1,
        "fused_score": 0.01639344262295082
      }
    }
  ],
  "effective_scope": {
    "workspace_id": "default",
    "project_id": "harbour",
    "include_workspace": true,
    "profile_id": "default",
    "source_ids": ["corpus:base"],
    "collection_ids": null,
    "research_types": null,
    "tags": [],
    "from_profile": ["source_ids", "collection_ids", "research_types", "tags"],
    "empty": false
  },
  "source_status": [
    {"schema_version": 1, "source_id": "corpus:base", "state": "ok", "error": null, "hit_count": 1, "elapsed_ms": 1}
  ],
  "partial": false,
  "warnings": [],
  "next_cursor": null
}
```

- A hit is identified by `source_id`, `record_id` and `revision_id`. The
  revision id identifies the record's content at search time; pass it to open.
- `ranking.native_*` is the source's own rank and score (`bm25`: lower is
  better; `cosine`: higher is better; `rrf`: a fused score). Scores of
  different sources are never compared. `fused_rank` and `fused_score` order
  the response: reciprocal rank fusion (k = 60) over the sources' rankings,
  with ties broken by hit identity, so the same request gives the same order.
- A record in several collections appears once, with every collection id.
- FTS excerpts mark matches with `<mark>…</mark>`; a hit found only by the
  embedding index has a plain excerpt.
- `warnings` reports cut hits (byte budget), a query searched as a phrase
  because it was not valid FTS5 syntax, and similar notes, each prefixed by
  its source id.

### Partial results and failures

| Situation | Result |
| --- | --- |
| No matches | Success, no hits, every source `ok` |
| Explicit empty selection (`"source_ids": []`) | Success, no hits, no sources |
| One source fails, times out or cannot serve the mode; others answer | Success with `partial: true`; that source's status is `unavailable`, `timeout` or `error` and carries an `error` |
| Every selected source fails | `unavailable` error (HTTP 503), never an empty success |
| `vector`/`hybrid` without an embedding index | The corpus reports `unavailable` (see [EMBEDDINGS.md](EMBEDDINGS.md)); FTS results are never relabelled as vector results |
| Unknown id, project collection in another project, denied principal | The whole request fails (`unknown_scope`, `invalid_scope`, `denied`) |

A source that misses the deadline is reported `timeout` and its late answer is
ignored. Errors use the shape described in
[SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md#errors).

## Opening a record

```json
{
  "context": {"workspace_id": "default", "project_id": "harbour"},
  "source_id": "corpus:base",
  "record_id": "file-796a77177822595e081f023469eef1d3",
  "revision_id": "rev_176a0a5ca0ab8e8ec174242a0c8d8d8e"
}
```

```json
{
  "schema_version": 1,
  "source_id": "corpus:base",
  "record_id": "file-796a77177822595e081f023469eef1d3",
  "revision_id": "rev_176a0a5ca0ab8e8ec174242a0c8d8d8e",
  "scope": {"workspace_id": "default", "project_id": "harbour"},
  "media_type": "text/markdown",
  "content": "# Tides\n\nSpring tides follow the new and full moon.\n",
  "locator": null,
  "verification": "verified",
  "digest": "sha256:eb715a3741d2407e05ed1a897b1a13cfb36a84e09a7eb17c201b704252e1c0ac",
  "provenance_id": "entry:notes",
  "source_status": {"schema_version": 1, "source_id": "corpus:base", "state": "ok", "error": null, "hit_count": 0, "elapsed_ms": null}
}
```

The record must be inside the context's scope. A record outside it is
reported like an unknown record (`unknown_scope`). Omit `revision_id` to open
the current content.

| `verification` | Meaning |
| --- | --- |
| `verified` | The requested revision is the current content; `content` and `digest` are returned |
| `unsupported` | The content changed since that revision. Older revisions are not stored yet, so nothing is returned; the latest content is never substituted. `locator.reason` names the current revision |
| `unavailable` | The file or document is gone |

An optional `locator` (`{"kind": "text", "start_byte": …, "end_byte": …}`,
UTF-8 bytes, end exclusive) is checked against the content and echoed.

## Project-context sources

Project-context documents become searchable when you link a project to the
project-context store of a repository on this host:

```bash
knowledge-engine knowledge project-context link default harbour /abs/path/to/repo [--project-fp proj_…]
knowledge-engine knowledge project-context unlink default harbour
```

HTTP: `PUT` / `DELETE /knowledge/workspaces/{workspace_id}/project-context/links/{project_id}`
(body `{"root": "/abs/path", "project_fp": null}`); MCP:
`knowledge_project_context_link` / `knowledge_project_context_unlink`.

- The first link creates the source `project_context:<workspace_id>`. The
  fingerprint defaults to the one registered for the root
  (`knowledge-engine project-context` must have registered the project).
- Only linked projects are searchable. One fingerprint can be linked to one
  project only.
- Each document belongs to its linked project, with research type `other`, no
  tags and no collections. A query constrained by collection or tag returns no
  project-context documents.
- Record ids are `<project_id>:<document id>`. The source supports `fts` only;
  in `vector`/`hybrid` mode it is reported `unavailable` and the response is
  partial.
- Open returns the stored sanitized text and honours
  `[project_context.mcp] allow_full_content` (`denied` when false).

The agent board is not a search source. A source of kind `board` is reported
`unavailable`.

## Limits

- At most `limit` candidates per source. Within the corpus, `hybrid` fuses at
  most 200 FTS and 200 vector candidates, so it approximates a full-corpus
  ranking.
- A filtered FTS query examines at most 50 000 ranked matches. When that cap
  is reached before `limit` in-scope hits are found, a warning says so.
- A search loads the metadata of every in-scope record (at most 1 000 000;
  larger scopes are `invalid_scope`: narrow them).
- Records larger than 16 MiB have no revision id and cannot be opened.
- Like every `/knowledge` route, these surfaces are local-trust.
  `KE_KNOWLEDGE_ENABLED=0` removes the HTTP routes and MCP tools (the CLI still
  works). See [DEPLOYMENT.md](DEPLOYMENT.md) before exposing them.
