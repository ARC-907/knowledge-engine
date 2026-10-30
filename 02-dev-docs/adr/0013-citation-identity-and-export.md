# ADR 0013 — Citation identity, claim validation and export formats

- **Status:** Accepted (V2 P4, issue #36)
- **Date:** 2026-10

## Context

Generated answers must cite evidence that survives reindexing and must not
present resolution as truth ([V2-DESIGN.md](../V2-DESIGN.md), citation
production). The first export formats are Markdown and CSL-JSON.

## Decision

- **Identity.** A citation is identified by its evidence id
  (`derive_evidence_id`: revision + canonical locator + excerpt digest). No
  row id or chunk ordinal appears in a citation. A reference cited by several
  claims is listed once.
- **Input.** A claim-to-evidence map: each claim is a `quotation` (with the
  verbatim `quote`) or a `paraphrase`, citing stored `evidence_id`s or
  explicit `source_id` + `record_id` + `revision_id` + `locator`. Quotations
  and paraphrases are reported and rendered separately.
- **Validation.** Every reference is opened against its exact revision. A
  claim is `resolved` when all its evidence is `verified` or `stale` (stale is
  flagged), `unresolved` otherwise, `unsupported` when it cites nothing. A
  quotation must equal each cited span exactly; paraphrases are never
  compared. Every report states that this is not proof of entailment.
  Validated explicit spans are remembered so a later deletion still resolves
  them to `unavailable`.
- **Scope.** The caller's scope is rechecked for every cited record; one
  out-of-scope reference fails the whole request with `denied`, so no
  metadata of it leaks.
- **Markdown** is deterministic: sections for quotations, paraphrases and
  claims without evidence with inline `[n]` references (`[unresolved]` marks
  unresolved evidence), then one paragraph per reference. Titles and other
  recorded text are Markdown-escaped; only `http(s)` URLs become autolinks
  (percent-encoded); locators are code spans in half-open notation.
- **CSL-JSON** items use the evidence id as `id`; absent author/date/title
  stay absent; authors are `literal` names (never split); only `http(s)`
  URLs are exported as `URL`; the revision, locator and verification state
  are in `note` and `custom`.
- **Surfaces.** `/knowledge/citations/validate|export`, CLI
  `knowledge cite validate|export`, and MCP `knowledge_cite_validate` /
  `knowledge_cite_export`, generated from the knowledge operation catalog so
  they use the reserved `knowledge_*` prefix of ADR 0009.

## Consequences

- Golden files in `engine/tests/fixtures/evidence/` pin both formats; other
  styles (BibTeX, CSL styles) need their own fixtures first.
- Validation never decides whether a claim is true.
