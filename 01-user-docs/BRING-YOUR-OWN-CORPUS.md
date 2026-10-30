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
  libraries/     <- domain knowledge libraries (most content goes here)  -> kind "library"
  skills/        <- skill packages (procedures an agent can follow)       -> kind "skill"
  kits/          <- reusable workflow bundles                             -> kind "kit"
  capabilities/  <- tool / integration reference docs                    -> kind "tool"
```

Each top-level folder under these four is one registry entry; `bootstrap` and
`watch` only look there. Content anywhere else under the corpus can still be
registered by adding an entry to `registry.json` by hand (`path` is relative
to the corpus root). A library is a folder
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
activated.) Setting `KE_HOME` alone is enough when the external location
follows the `corpus/registry.json` layout; otherwise set `KE_CORPUS_ROOT` and
`KE_REGISTRY_PATH` as above. The same variables apply to `serve`, `mcp` and
every other command, and to MCP client configs (see
[MCP-WIRING.md](MCP-WIRING.md)).

The registry file will be created at `KE_REGISTRY_PATH` on first run if it
doesn't exist.

## The registry is the source of truth

`registry.json` decides what is indexed and what the dashboard and MCP server
expose. Key facts:

- It's validated against the registry JSON Schema
  (`corpus/registry.schema.json`) every time the engine loads or writes it.
- Each entry has an `enabled` flag — disabled entries are not indexed.
- The dashboard's toggle switches, and the MCP `registry_toggle` tool, flip
  that flag.
- `bootstrap` adds entries; it never removes them. To remove an entry, delete
  it from the JSON, use the dashboard, or call the registry API.

You can edit `registry.json` by hand — it's plain JSON. Just keep it valid
against the schema.

### Nested entries

Entries may nest — for example a library at `libraries/example` and a second
entry at `libraries/example/research`. Each file then belongs to exactly one
entry, the **deepest enabled** one containing it, in every search mode: a
search returns the file once, under that entry. A disabled nested entry hands
its files back to the enclosing enabled entry, and a `kind` filter matches the
owning entry's kind. See
[ADR 0005](../02-dev-docs/adr/0005-nested-entry-ownership.md).

### Validate your registry

After a hand edit, check the file before starting the engine:

```bash
knowledge-engine registry validate                      # the configured KE_REGISTRY_PATH
knowledge-engine registry validate path/to/registry.json
knowledge-engine registry validate --json               # machine-readable report
```

It exits `0` when the file is valid and `1` otherwise, printing one line per
problem with its JSON pointer (for example
`/libraries/0/tags: 'a,b' is not of type 'array'`). Unknown top-level keys are
rejected; entries need `id`, `name` and `path`, and may carry extra fields of
your own. `serve`, `mcp`, `bootstrap`, `reindex` and the other corpus commands
refuse to start on an invalid registry and report the same errors. The schema
ships inside the package; `corpus/registry.schema.json` is an identical copy,
and adding `"$schema": "https://raw.githubusercontent.com/ARC-907/knowledge-engine/main/corpus/registry.schema.json"`
to your `registry.json` gives editors completion and inline errors.

## Auto-registering new folders

`knowledge-engine watch` watches the corpus root and registers a new
top-level folder under `libraries/`, `skills/`, `kits/` or `capabilities/` the
moment it appears — no manual `bootstrap` needed. This is convenient for an
actively growing corpus; leave `watch` off for a static one. It is a
long-running command, so it logs at `KE_LOG_LEVEL` (INFO) by default.

The registry also carries a `lifecycle` block (`watch_enabled`,
`auto_register`, both on by default), readable and writable through `GET` /
`PATCH /registry/lifecycle/state` and the dashboard. A running `watch` re-reads
`registry.json` on every new folder, so a change made through the server takes
effect immediately: with either flag off, new folders are logged and left
unregistered until the flag is turned back on (`bootstrap` still registers
everything on demand).

## Sizing expectations

FTS5 is fast and the index is compact. A corpus of a few thousand markdown
files indexes in seconds and searches in milliseconds. There is no practical
need for embeddings at small scale — FTS5 lexical search is enough. If you want
semantic ("find by meaning") search on top, see [EMBEDDINGS.md](EMBEDDINGS.md).
