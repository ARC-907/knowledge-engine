# ADR 0005 — Nested registry entries: the deepest enabled entry owns a file

- **Status:** Accepted
- **Date:** 2026-10
- **Issue:** [#39](https://github.com/ARC-907/knowledge-engine/issues/39)

## Context

Registry entries can nest: a library at `libraries/example` and another
entry at `libraries/example/research`. Until this decision the search modes
disagreed about which entry a nested file belonged to:

- FTS indexing walked every enabled entry recursively and stored one row per
  entry and file, so a nested file was indexed under the parent **and** the
  child (membership).
- Vector search attributed each file to the longest matching entry path
  (ownership), but applied a `kind` filter *before* choosing, so a filtered
  search re-attributed a nested skill's file to its enclosing library.
- Hybrid fusion deduplicated on `(entry_id, path)`, so the parent-owned FTS
  copy and the child-owned vector copy survived as two hits for one file.

A reproduction fixture confirmed all three: FTS and hybrid returned the same
file twice, and kind filters disagreed between modes.

## Decision

Nesting means **ownership**. Every file has at most one owner:

1. The owner is the deepest *enabled* registry entry whose path is the file
   itself or a directory containing it.
2. A disabled entry owns nothing; its files fall back to the nearest enabled
   ancestor, and are not searchable at all when there is none.
3. A `kind` filter selects files whose owner has that kind. It never
   re-attributes a file to an enclosing entry of the requested kind.
4. Paths compare as corpus-relative POSIX strings after `os.path.normcase`.
5. Two entries with exactly the same path: the first in registry order owns
   the files (registry validation should prevent this; ownership stays
   deterministic if it does not).

One implementation, `knowledge_engine.retrieval.ownership.EntryOwnership`,
is used by FTS indexing, vector search and hybrid fusion.

Membership — a file belonging to several groupings at once — is the job of
V2 collections ([V2-DESIGN.md](../V2-DESIGN.md)), which are many-to-many and
orthogonal to registry entries. Entries describe packaging, not taxonomy.

## Consequences

- A search returns a file at most once, with the same `entry_id` in `fts`,
  `vector` and `hybrid` mode.
- **Compatibility:** an FTS search that used to return a nested file under its
  parent entry now returns it only under the nested entry, and `reindex`
  reports fewer `files` when entries nest (each file counts once). A
  `kind=library` search no longer returns files owned by a nested skill or
  kit. Corpora without nested entries are unaffected.
- Run `knowledge-engine reindex` after upgrading to apply the rule to an
  existing FTS index; the embedding index needs no rebuild (it stores files,
  not entries).
