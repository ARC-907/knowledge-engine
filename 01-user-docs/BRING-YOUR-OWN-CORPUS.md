# Bring Your Own Corpus

Knowledge-Engine is corpus-agnostic: the engine code hardcodes no library
names, paths, or counts. The demo library and demo skill that ship with the
repo are *examples* — your job is to replace them with your own content.

This guide covers both ways to do that.

## Two approaches

| Approach | When to use it |
| --- | --- |
| **Drop into the shipped layout** | The common case. Put your markdown under `corpus/`, register it, reindex. |
| **Point at an external corpus** | You already have a markdown repo elsewhere and don't want to move it. Set env vars so the engine reads it in place. |

## Approach 1 — drop into the shipped layout

### Step 1: decide what to keep

The repo ships a demo library and demo skill under `corpus/samples/`. You can:

- **Keep them** and add yours alongside — fine while you learn the patterns.
- **Delete them** for a clean corpus — recommended once you're comfortable.

To remove them: delete `corpus/samples/demo-library/` and
`corpus/samples/demo-skill/`, then remove the `library-demo` and `skill-demo`
entries from `corpus/registry.json` (or just set their `enabled` flags to
`false` to keep them out of the index without deleting anything).

### Step 2: add your content

Drop folders into the corpus subdirectories that fit:

```
corpus/
  libraries/     <- domain knowledge libraries (most content goes here)
  skills/        <- skill packages (procedures an agent can follow)
  kits/          <- reusable templates
  capabilities/  <- tool / integration reference docs
  governance/    <- workflow and process docs
```

Each top-level folder under these is one registry entry. A library is a folder
with a `README.md`, a `CATALOG.md`, and numbered topic subfolders — see
[LIBRARY-AUTHORING.md](LIBRARY-AUTHORING.md). A skill is a folder with a
`SKILL.md` — see [SKILL-AUTHORING.md](SKILL-AUTHORING.md).

Only `.md`, `.txt`, and `.rst` files are indexed. Other files (images, data)
can live in the corpus but won't be searchable.

### Step 3: register and index

```
knowledge-engine bootstrap   # registers every new folder it finds
knowledge-engine reindex     # rebuilds the FTS5 index over enabled entries
```

`bootstrap` is idempotent — it skips folders already in the registry and adds
only new ones. `reindex` always rebuilds from scratch, so it's safe to run any
time content changes.

### Step 4: verify

```
knowledge-engine info                 # confirm the new counts
knowledge-engine search "some term"   # confirm your content is searchable
```

Or open the dashboard and search there.

## Approach 2 — point at an external corpus

If your markdown lives in another repository or folder, you don't have to move
it. The engine reads every path from environment variables:

| Env var | What it controls | Default |
| --- | --- | --- |
| `KE_HOME` | Base for the defaults below | nearest directory (from the cwd upward) containing `corpus/registry.json`, else the cwd |
| `KE_CORPUS_ROOT` | Where libraries / skills / kits live | `${KE_HOME}/corpus` |
| `KE_DATA_DIR` | Where the SQLite search index (and other runtime databases) are written | `${KE_HOME}/.knowledge-engine/data` |
| `KE_REGISTRY_PATH` | The registry JSON file | `${KE_CORPUS_ROOT}/registry.json` |

Set them before running any command:

```pwsh
# Windows
$env:KE_CORPUS_ROOT = "C:\path\to\my\markdown"
$env:KE_REGISTRY_PATH = "C:\path\to\my\markdown\registry.json"
knowledge-engine bootstrap
knowledge-engine reindex
knowledge-engine serve
```

```bash
# macOS / Linux
export KE_CORPUS_ROOT="/path/to/my/markdown"
export KE_REGISTRY_PATH="/path/to/my/markdown/registry.json"
knowledge-engine bootstrap
knowledge-engine reindex
knowledge-engine serve
```

(Prefix each command with `uv run --project engine` if the venv is not
activated.) `KE_CORPUS_ROOT`, `KE_DATA_DIR`, and `KE_REGISTRY_PATH` default to
the checkout's `corpus/` layout; set them before `serve` to point at an
external corpus.

The registry file will be created at `KE_REGISTRY_PATH` on first run if it
doesn't exist.

## The registry is the source of truth

`registry.json` decides what is indexed and what the dashboard and MCP server
expose. Key facts:

- It's validated against `corpus/registry.schema.json`.
- Each entry has an `enabled` flag — disabled entries are not indexed.
- The dashboard's toggle switches, and the MCP `registry_toggle` tool, flip
  that flag.
- `bootstrap` adds entries; it never removes them. To remove an entry, delete
  it from the JSON, use the dashboard, or call the registry API.

You can edit `registry.json` by hand — it's plain JSON. Just keep it valid
against the schema.

## Auto-registering new folders

The registry has a `lifecycle` block. If `auto_register_new_folders` is `true`
and you run `knowledge-engine watch`, the engine watches the corpus root and
registers folders the moment they appear — no manual `bootstrap` needed. This
is convenient for an actively-growing corpus; leave `watch` off for a static
one.

## Sizing expectations

FTS5 is fast and the index is compact. A corpus of a few thousand markdown
files indexes in seconds and searches in milliseconds. There is no practical
need for embeddings at small scale — FTS5 lexical search is enough. If you want
semantic ("find by meaning") search on top, see [EMBEDDINGS.md](EMBEDDINGS.md).
