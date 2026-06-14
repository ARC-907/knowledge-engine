# Skill Authoring

A *skill* is a packaged procedure — a folder of markdown describing a task an
agent (or a person) can carry out. Where a library is *knowledge*, a skill is
*know-how*. This guide shows how to author one.

## What a skill is for

A skill answers "how do I do X." It's the unit you reach for when you want a
repeatable procedure indexed and discoverable. The engine treats skills as a
distinct registry `kind`, so you can search them on their own (the dashboard's
kind filter, or the MCP `search` tool with `kind: "skill"`).

The template ships one example: `corpus/samples/demo-skill/`. It is
intentionally minimal — a starting shape, not a model of depth.

## Skill folder structure

```
corpus/skills/my-skill/
  SKILL.md          <- the entry point; required
  procedure.md      <- the step-by-step (optional but conventional)
  <reference>.md    <- supporting references, examples (optional)
```

The only hard requirement is a `SKILL.md` at the folder root. Everything else
is convention.

## SKILL.md

`SKILL.md` carries frontmatter and a body. Conventional frontmatter:

```yaml
---
name: my-skill
description: One sentence describing what the skill does and when to use it.
---
```

The `description` is what a reader scanning search results sees first — make it
specific. "Reconcile two CSV exports and flag mismatches" beats "data tool."

A workable body, following the demo skill's shape:

```markdown
# My Skill

## When to use

The trigger conditions — when this skill is the right one to reach for.

## How it works

1. First step.
2. Second step.
3. ...

## Inputs / outputs

- **Input**: what the skill consumes.
- **Output**: what it produces.
```

## Supporting files

Anything else in the folder is indexed alongside `SKILL.md`. Common companions:

- `procedure.md` — the detailed step-by-step, if `SKILL.md` only summarizes.
- `examples.md` — worked examples.
- reference files — checklists, templates, decision tables.

All `.md` / `.txt` / `.rst` files in the folder become searchable.

## Declaring external dependencies

If a skill needs something the engine can't provide — an API key, a CLI tool,
another MCP server — record it in the skill's registry entry under
`external_deps`:

```json
{
  "id": "skill-my-skill",
  "kind": "skill",
  "name": "My Skill",
  "path": "skills/my-skill",
  "enabled": true,
  "external_deps": ["Tavily API key", "ripgrep on PATH"]
}
```

`external_deps` is free text and the engine does not enforce it — it exists so
the dependency is *visible* to whoever uses the skill. The dashboard's skill
detail surfaces it.

## Register and index

```
knowledge-engine bootstrap   # registers skills/my-skill as kind "skill"
knowledge-engine reindex     # indexes the skill's files
```

`bootstrap` assigns the entry id `skill-<slug-of-folder-name>` and `kind:
skill`. After `reindex`, the skill is full-text searchable and shows up under
the dashboard's skill filter.

## Skills vs. libraries — which one?

| Use a **library** when… | Use a **skill** when… |
| --- | --- |
| You're capturing domain knowledge | You're capturing a repeatable procedure |
| The content is reference material | The content is "here's how to do it" |
| It has topics and sub-topics | It has steps |
| An agent reads it to *understand* | An agent reads it to *act* |

Many corpora have both: libraries for the knowledge, skills for the procedures
that operate on it.

## Checklist

- [ ] Folder under `corpus/skills/`, kebab-case name
- [ ] `SKILL.md` at the root with `name` + `description` frontmatter
- [ ] A clear "When to use" trigger section
- [ ] Steps written so they can actually be followed
- [ ] `external_deps` recorded in the registry entry if anything is required
- [ ] `bootstrap` + `reindex` run; skill confirmed searchable under kind `skill`
