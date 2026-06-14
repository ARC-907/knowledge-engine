# Library Authoring

A *library* is a domain knowledge collection — a folder of markdown organized
so that both humans and AI agents can navigate it. This guide shows how to
author one well, using the **three-lens framework** that the bundled
demonstration libraries are built on.

## Why structure matters

A pile of markdown files is searchable but not *navigable*. The difference
between slop research and usable research is organization: a corpus an agent
can reason about, where every file declares where it sits and what lens it
belongs to.

The three-lens framework is the organizing discipline. It is simple, it is the
same across every library, and it survives scale.

## The three-lens framework

Every library declares **three categorical perspectives** — three "lenses" —
tuned to that library's domain. The framework is constant; the lenses adapt.

The shipped `demo-library` declares **CONCEPT → PATTERN → APPLY**. Different
domains pick different lenses — for example:

| Domain | Three-lens framework |
| --- | --- |
| Decision analysis | **FRAME → ANALYZE → DECIDE** |
| AI monetization | **IDENTIFY → EVALUATE → EXECUTE** |
| System design | **FRAME → DESIGN → EVOLVE** |

A lens is a *stage of thought* about the domain. For a decision library:
frame the problem, analyze it, commit. For a design library: frame the
constraints, design the system, evolve it. Pick three that genuinely partition
how work in your domain proceeds — not three arbitrary buckets.

A good lens triad is:

- **Sequential or categorical** — the lenses are stages, or distinct modes,
  not overlapping topics.
- **Domain-tuned** — they name how *this* domain's work actually flows.
- **Memorable** — three words a reader internalizes after one pass.

When in doubt, read the demo library's `README.md`. It opens by declaring and
explaining its lenses.

## Library folder structure

```
corpus/libraries/My Domain/
  README.md            <- declares the three-lens framework; the entry point
  CATALOG.md           <- inventory of every file in the library
  00-foundations/      <- core concepts the rest of the library depends on
    README.md
    <topic>.md
  01-<topic>/          <- first domain topic
    README.md
    <topic>.md
  02-<topic>/          <- second domain topic
    ...
```

- **Numbered topic folders** enforce a reading order. `00-foundations/` always
  comes first — it holds the primitives everything else builds on.
- **Every folder gets a `README.md`** describing its scope.
- **Folder names are kebab-case**, prefixed with a two-digit number.

## The library README

The README is the most important file — it's what a reader (and an agent) hits
first. Structure it like the bundled libraries:

```markdown
# My Domain

> **Three-Lens Framework:** LENS-A → LENS-B → LENS-C
> **Classification:** Subject-agnostic reference library

## Purpose

One paragraph: what this library is for and what problem it solves.

## The Three Lenses

- **LENS-A** — what this lens is, what work happens here.
- **LENS-B** — ...
- **LENS-C** — ...

## Structure

| Folder | Scope |
| --- | --- |
| 00-foundations/ | ... |
| 01-<topic>/ | ... |

See CATALOG.md for the full file inventory.
```

## Content file frontmatter

Every content `.md` file carries YAML frontmatter. At minimum:

```yaml
---
title: "Human-Readable Title"
category: "00-foundations"
status: draft
created: 2026-01-15
last_updated: 2026-01-15
---
```

Useful optional fields, used by the bundled libraries:

| Field | Purpose |
| --- | --- |
| `status` | `stub` → `draft` → `complete`. Tracks maturity. |
| `perspective` | Which lens this file primarily serves (e.g. `FRAME → ANALYZE → DECIDE`). |
| `depends_on` | List of files this one assumes you've read. |
| `related` | List of sibling files worth reading alongside. |
| `research_priority` | `P1` / `P2` / `P3` — your own backlog signal. |

Frontmatter is indexed along with body text, so a precise `title` and
`description` improve search hits.

## Writing the content body

A consistent file shape helps agents extract structure. The bundled libraries
use roughly:

1. **`> blockquote` summary** — one or two sentences stating what the file
   covers. This is what shows in search snippets.
2. **`## Overview`** — the longer framing.
3. **`## Key Concepts`** — the substance, with `### LENS —` subheadings that
   tie sections back to the three-lens framework.
4. **`## Decision Rules`** (or similar) — actionable `IF … THEN …` guidance.
5. **`## Cross-References`** — links to related files *within this corpus*.
6. **`## Sources`** — citations.

You don't have to copy this exactly — but a predictable shape pays off when an
agent reads dozens of files.

## Cross-references

Link between files with **relative markdown links**, e.g.
`[Decision-Making Taxonomy](../00-foundations/decision-making-taxonomy.md)`.
Keep cross-references inside the corpus — don't point at files or systems that
won't ship with the corpus, or readers hit dead ends.

## CATALOG.md

`CATALOG.md` is a flat inventory of the library: every file, its status, a
one-line description. It's the table of contents an agent reads to plan a
traversal. Keep it current as you add files.

## Register and index

Once the folder exists:

```
knowledge-engine bootstrap   # registers the new library
knowledge-engine reindex     # indexes its files
```

The library's `framework` field in `registry.json` is set from the three-lens
declaration — `bootstrap` registers the folder; you can add the `framework`
string to the registry entry so the dashboard displays it.

## Checklist

- [ ] Folder under `corpus/libraries/`, kebab-case name
- [ ] `README.md` declaring a domain-tuned three-lens framework
- [ ] `CATALOG.md` inventory
- [ ] Numbered topic folders, `00-foundations/` first
- [ ] Every folder has a `README.md`
- [ ] Every content file has YAML frontmatter
- [ ] Cross-references are relative and stay inside the corpus
- [ ] `bootstrap` + `reindex` run; content confirmed searchable
