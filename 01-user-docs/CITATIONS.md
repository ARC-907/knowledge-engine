# Evidence and citations: revisions, open, claim validation and export

Knowledge-Engine can keep the exact bytes a statement was based on, point at
a precise span of them, and turn a set of claims into checked references.
Everything lives in the scoped-knowledge store `knowledge.db` (see
[SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md)); run `knowledge migrate --apply`
first.

What it checks: that cited evidence still resolves to its exact revision, and
that quotations match their spans character for character. What it does
**not** check: whether the evidence actually supports a claim. Every
validation and export says so. Verification states describe evidence
resolution, never the truth of a claim.

## The model

| Thing | What it is | Identity |
| --- | --- | --- |
| Record | A file or document owned by a source (`corpus:base` for the corpus) | `source_id` + `record_id`; a rename keeps it and records the old path as an alias |
| Revision | The exact bytes of a record at one time, with media type, observed time and upstream version/date | `rev_…`, derived from source, record and SHA-256 of the bytes; reindexing never changes it |
| Evidence span | A typed locator into one revision plus the digest of its excerpt | `ev_…`, derived from revision, locator and excerpt digest |
| Derivation | Which module run produced a revision from which parents | the run id |
| Tombstone | What deletion leaves: identity, digest, time and reason | the revision id |

Identical bytes in two records are two revisions: content identifies bytes,
not ownership. An edit creates a new revision; the old one stays retained so
older citations keep resolving (as `stale`).

### Locators

All ranges are half-open `[start, end)`.

| Kind | Position | Notes |
| --- | --- | --- |
| `text` | UTF-8 **byte** offsets into the exact revision | Must fall on character boundaries; `heading` is informational. Markdown sections start at a heading line and end at the next heading of any level |
| `transcript` | `start_ms`, `end_ms` (integer milliseconds), optional `speaker` | From a JSON artifact `{"segments": [{"start_ms", "end_ms", "speaker", "text"}]}`; a locator must align with segment boundaries |
| `code` | repository id, `commit` **or** `snapshot_digest`, path, optional qualified symbol, 1-based lines with exclusive end | A dirty tree is a snapshot digest, never a commit |
| `pdf` | 1-based page | The engine ships no PDF text extractor, so PDF positions are always `unsupported`; pages are never invented |
| `unsupported` | a reason | When no position can be recovered |

## Recording the corpus: `knowledge snapshot`

```bash
knowledge-engine knowledge snapshot --dry-run     # report; writes nothing
knowledge-engine knowledge snapshot --apply       # record new revisions
knowledge-engine knowledge snapshot --apply --delete-missing
```

The snapshot walks the corpus like `reindex` and, per file:

- **unchanged** bytes: nothing is written (reruns are no-ops);
- **edited** bytes: a new revision becomes current;
- **renamed** file (a vanished record's exact bytes appear at exactly one new
  path): the record moves and keeps its id; the old path becomes an alias.
  Ambiguous cases are not guessed: they appear as new plus missing;
- **missing** file: reported; deleted only with `--delete-missing`, and never
  while a registry entry's content is missing (an unmounted folder must not
  erase evidence);
- files larger than `KE_EVIDENCE_MAX_BYTES` are recorded **digest-only** with
  the reason.

Markdown files get one evidence span per heading section; `.txt`/`.rst`
files one span for the whole document. Run `knowledge status` to see the
evidence counts and the pending outbox (index work for other stores).

## Opening evidence

```bash
knowledge-engine knowledge evidence revisions corpus:base <record_id>
knowledge-engine knowledge evidence open --evidence-id ev_…
knowledge-engine knowledge evidence open --source-id corpus:base \
    --record-id <record_id> --revision-id rev_… \
    --locator '{"kind": "text", "start_byte": 12, "end_byte": 35}'
```

HTTP: `POST /knowledge/evidence/open` with `{"context": {...}, ...}`; MCP:
`knowledge_evidence_open`. Every open rechecks the scope (`denied` outside
it), loads the **exact** cited revision, verifies its digest and the span,
and answers:

| State | Meaning | Content returned |
| --- | --- | --- |
| `verified` | The cited revision is current and the span is addressable | yes |
| `stale` | A newer revision exists; the cited revision is still retained | yes (the cited text, never the latest) |
| `unavailable` | Bytes deleted, digest-only, or failing the digest check | no |
| `ambiguous` | The locator matches several positions (e.g. a symbol defined twice) | no |
| `unsupported` | No usable locator (PDF, range outside the revision, binary media) | no |

The dashboard's Knowledge tab opens records and evidence and exports
citations of a whole revision or a selected quotation
([DASHBOARD.md](DASHBOARD.md#knowledge-opening-records-and-citing-them)).

## Validating claims and exporting citations

A request is a claim-to-evidence map in one scope:

```json
{
  "context": {"workspace_id": "default"},
  "format": "both",
  "claims": [
    {"claim_id": "q1", "kind": "quotation", "quote": "Run the tool with care.",
     "evidence": [{"source_id": "corpus:base", "record_id": "file-…",
                   "revision_id": "rev_…",
                   "locator": {"kind": "text", "start_byte": 12, "end_byte": 35}}]},
    {"claim_id": "p1", "kind": "paraphrase", "text": "Care is advised.",
     "evidence": [{"evidence_id": "ev_…"}]}
  ]
}
```

```bash
knowledge-engine knowledge cite validate claims.json
knowledge-engine knowledge cite export claims.json --format markdown --raw
knowledge-engine knowledge cite export claims.json --format csl-json --raw > refs.json
```

HTTP: `POST /knowledge/citations/validate` and `POST /knowledge/citations/export`;
MCP: `knowledge_cite_validate` and `knowledge_cite_export`.

- A claim is `resolved` when all its evidence is `verified` or `stale` (stale
  is flagged), `unresolved` when some evidence does not resolve or a quotation
  does not match its span exactly, and `unsupported` when it cites nothing.
- Quotations and paraphrases are reported and rendered separately;
  paraphrases are never compared with the text.
- Any cited record outside the request's scope fails the whole request with
  `denied` (HTTP 403), so nothing about it leaks.
- References are deduplicated by evidence identity. Markdown is
  deterministic: titles are escaped, only `http(s)` URLs become links,
  missing authors/dates are simply absent. CSL-JSON items use the evidence id
  as `id`, keep absent fields absent and carry the revision, locator and
  verification state in `note`/`custom`.
- A validated span is remembered, so after its file is deleted the citation
  resolves `unavailable` instead of disappearing.

## Deleting evidence

```bash
knowledge-engine knowledge evidence delete corpus:base <record_id> --reason "rights request" --yes
```

`KE_EVIDENCE_ON_DELETE=tombstone` (default) removes the bytes and ingestion
records and keeps a minimal tombstone (identity, digest, time, reason):
citations then resolve `unavailable`. `purge` removes the record, its
revisions, spans and aliases entirely: citations no longer resolve at all.
Deletion takes precedence over reproducibility; backups taken by
`knowledge migrate --apply` are operator-owned copies and must be handled by
the same policy.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `KE_EVIDENCE_ON_DELETE` | `tombstone` | `tombstone` or `purge` (see above) |
| `KE_EVIDENCE_RETENTION` | `retain` | Retention of records the evidence store creates: `retain` or `digest_only` |
| `KE_EVIDENCE_MAX_BYTES` | `16777216` | Largest revision whose bytes are retained; larger ones are digest-only |

See [CONFIGURATION.md](CONFIGURATION.md) for every setting and
[ADR 0012](../02-dev-docs/adr/0012-evidence-store-and-ingestion.md) /
[ADR 0013](../02-dev-docs/adr/0013-citation-identity-and-export.md) for the
design decisions.
