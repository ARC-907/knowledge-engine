# Serena adapter (optional, read-only code symbols)

The engine can answer code questions about your repositories through
[Serena](https://github.com/oraios/serena), an MCP server that finds symbols
and references with a language server. The adapter is **off by default**,
**read-only**, and keeps every project in its own Serena process. Ordinary
knowledge search never depends on it.

## Two ways this repository uses Serena

| | Contributor tooling | The engine's adapter (this page) |
| --- | --- | --- |
| What | Serena on *this* checkout, for coding agents working on the engine | Serena on *your* repositories, as a knowledge source |
| Start | `uvx nox -s serena` ([DEV-TOOLING.md](../02-dev-docs/DEV-TOOLING.md#serena)) | `KE_SERENA_ENABLED=1` plus a project file |
| Tools | Serena's full tool set, including editing | Three read tools only |
| Routing | The checkout | One process per configured project id |

## Install Serena separately

Serena is licensed **GPL-3.0-or-later** (its package metadata). The engine is
MIT and **never bundles, vendors or installs** Serena. You install it next to
the engine and point the adapter at it:

```bash
uv tool install serena-agent==2.1.0        # or: pipx install serena-agent==2.1.0
export KE_SERENA_COMMAND="$(command -v serena)"
```

`KE_SERENA_COMMAND` may also be a command line, for example
`uvx --from serena-agent==2.1.0 serena`.

Requirements:

- `uv` on `PATH`: Serena starts its Python language server (pyright 1.1.403)
  through `uvx`. The adapter adds the directory of `uv` to Serena's `PATH`
  when the engine can find it.
- Tested language: **Python**.
- Tested release: **Serena 2.1.0**, MCP protocol `2026-07-28`.

## Configure projects

The project file (`KE_SERENA_PROJECTS`, TOML or JSON; default
`<data_dir>/serena-projects.toml`) maps scoped-knowledge projects
([SCOPED-KNOWLEDGE.md](SCOPED-KNOWLEDGE.md)) to repositories:

```toml
[[projects]]
project_id = "p-app"                 # an existing project id in knowledge.db
root = "/srv/repos/app"              # absolute path of the git repository
repository_id = "example.org/app"    # optional; default: the root directory's name

[[projects]]
project_id = "p-tools"
root = "/srv/repos/tools"
```

Rules:

- One entry per project, and one project per root. A malformed entry is
  reported and skipped.
- Requests are routed by `project_id` only. The current directory never picks
  a project, and Serena's own `activate_project` is never called.

Then enable the adapter and register the broker sources:

```bash
export KE_SERENA_ENABLED=1
knowledge-engine serena status      # configuration and session states (starts nothing)
knowledge-engine serena check       # start each project's Serena and verify it
knowledge-engine serena sync        # register serena:<project_id> sources in knowledge.db
```

The server and the broker register the sources on start, too.

| Setting | Default | Meaning |
| --- | --- | --- |
| `KE_SERENA_ENABLED` | `0` | Turn the adapter on |
| `KE_SERENA_COMMAND` | `serena` | The Serena executable or command line |
| `KE_SERENA_VERSION` | `2.1.0` | The version the server must report |
| `KE_SERENA_PROJECTS` | `<data_dir>/serena-projects.toml` | The project file |
| `KE_SERENA_REQUEST_TIMEOUT_S` | `60` | Deadline of one tool call |
| `KE_SERENA_STARTUP_TIMEOUT_S` | `300` | Deadline for starting Serena and its language server |
| `KE_SERENA_MAX_SESSIONS` | `4` | Most Serena processes at once |
| `KE_SERENA_IDLE_SHUTDOWN_S` | `900` | Stop an idle process after this many seconds (`0`: never) |

See [CONFIGURATION.md](CONFIGURATION.md) for the generated reference.

## What the adapter does

**Launch.** Each project gets its own Serena process, started on first use:

```
<KE_SERENA_COMMAND> start-mcp-server --transport stdio --context agent
    --agent-interface tools --enable-web-dashboard false --open-web-dashboard false
    --enable-gui-log-window false --log-level WARNING --project <root>
```

- `--agent-interface tools` is forced. Serena's default `agent` context
  otherwise offers only a code-execution REPL tool.
- The process runs with `SERENA_USAGE_REPORTING=false`. Without it, Serena
  reports usage to its vendor at startup.
- `HOME` is an isolated directory under `<data_dir>/serena/home/<project_id>`.
  Serena's global configuration, logs and downloads stay there, apart from
  your own.

**Compatibility check.** On every start the adapter compares the server with
the recorded witness:

- the server name (`Serena`) and the version (`KE_SERENA_VERSION`);
- the SHA-256 of the input schema of each tool it uses.

Any difference makes that project **`incompatible`**: no tool is called, and
`serena status` names the problems. After upgrading Serena, run
`serena check`. If only the version changed and the schemas still match,
setting `KE_SERENA_VERSION` to the new version is enough. Schema drift needs
a new engine release.

**Read-only tools.** Only these are forwarded:

| Engine tool (MCP) | CLI | Serena tool |
| --- | --- | --- |
| `serena.find_symbol` | `serena find-symbol <project> <pattern> [--path] [--substring] [--body]` | `find_symbol` |
| `serena.get_symbols_overview` | `serena overview <project> <path>` | `get_symbols_overview` |
| `serena.find_referencing_symbols` | `serena references <project> <name_path> <path>` | `find_referencing_symbols` |
| `serena.status` | `serena status` | (none) |

Everything else is refused before any process is touched:

- shell execution, file creation and all editing, renaming and deleting tools;
- memories and onboarding;
- `activate_project`;
- the REPL tool;
- any tool a future Serena adds.

A tool Serena offers beyond the recorded list appears in `serena status`
under `extra_upstream_tools` and is still never forwarded.

**Project isolation.**
- Every path you pass, and every path Serena returns, is resolved against the
  project's root, with symlinks followed. Results outside the root are dropped
  and listed under `rejected_paths`.
- Calls to one project are serialized. Calls to different projects run in
  parallel in separate processes.

**Failures.**

| Situation | Result |
| --- | --- |
| A call exceeds the timeout | `timeout`; the process is restarted on the next call |
| Serena exits | `unavailable` |
| Restarts after failures | Wait 1, 2, 4 … 60 s; meanwhile calls fail with a retryable `unavailable` that names the time left |
| All sessions busy at the cap | `unavailable`; an idle session is stopped to make room when one exists |

**Memories.** Serena memories are not imported into the engine.

## Code locators and citations

Results carry **code locators** ([ADR 0007](../02-dev-docs/adr/0007-record-revision-identity-and-locators.md)):

- **Identity:** the repository id and the repository-relative path; the
  symbol is written `Class.method`.
- **Lines:** 1-based with an exclusive end. Serena's 0-based inclusive
  `body_location` is converted (`start + 1`, `end + 2`).
- **Version:**
  - A clean working tree is pinned to its **commit** (`git rev-parse HEAD`).
  - A dirty tree (any change or untracked file) is pinned to a **snapshot
    digest** over the changed files and `HEAD`. Uncommitted content is never
    presented as a commit. Serena's own `.serena/` directory, which it writes
    into every project it opens, does not count as a change.
  - A directory that is not a git work tree gets an `unsupported` locator.

As a knowledge source, each configured project is `serena:<project_id>`
([KNOWLEDGE-SEARCH.md](KNOWLEDGE-SEARCH.md)):

- **Selection:** it is searched only when a query names it in
  `context.source_ids`, with mode `fts`. Ordinary workspace-wide searches
  neither include it nor start Serena.
- **Hits:** a hit is the file holding the symbol, with a content-addressed
  revision id, the symbol's code locator and its lines as the excerpt.
- **Open:** opening a hit at its revision serves the file, or the locator's
  lines, while the file still has that revision.
  - The opened revision is recorded in the evidence store with its commit or
    snapshot. You can then validate and export it with the citation tools
    ([CITATIONS.md](CITATIONS.md)) like any other evidence.
  - Once the file changes, the recorded revision opens as `stale`. A revision
    that was never opened is `unsupported`; newer content is never
    substituted.

```bash
knowledge-engine knowledge search --workspace ws --source serena:p-app "Shape"
knowledge-engine serena find-symbol p-app "Shape/area" --body
```

## Compatibility record

| Item | Recorded value |
| --- | --- |
| Serena | 2.1.0 (`serena-agent` on PyPI), license GPL-3.0-or-later |
| MCP protocol | `2026-07-28` (SDK client 2.3.0) |
| Language server | pyright 1.1.403 via `uvx` (Python) |
| Mapped tools | `find_symbol`, `get_symbols_overview`, `find_referencing_symbols` (schema hashes in `knowledge_engine/serena/witness.json`) |
| Result format | JSON text; `body_location` lines 0-based inclusive |
| Verified | Two projects with identical symbol names answered from their own repositories (opt-in live test) |
