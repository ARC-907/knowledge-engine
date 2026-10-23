# Contributing

Thanks for your interest in Knowledge-Engine. This page covers the local
setup, the checks a change must pass, how commits and pull requests are
organised, and the compatibility rules we follow. Please also read the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

You need [`uv`](https://docs.astral.sh/uv/) and, optionally,
[`just`](https://just.systems) (every recipe is a plain `uv` command you can
copy from the `justfile`).

```bash
git clone https://github.com/ARC-907/knowledge-engine
cd knowledge-engine
uv sync --project engine --all-groups      # engine/.venv with every dependency group
uv run --project engine pre-commit install # git hooks: ruff, zizmor, mypy, lockfile, whitespace
```

`just install` and `just hooks` do the same two steps. The engine package
lives in `engine/`; run tools with `uv run --project engine --directory engine …`
or through the `just` recipes from the repository root.

Node 22+ is needed only to rebuild the dashboard stylesheet
(`just ui-css`, see [THEMING.md](01-user-docs/THEMING.md)).

## The gate

Every pull request must pass the same checks CI runs. Locally:

```bash
just check          # lint (ruff format --check, ruff check), typecheck (mypy), test (pytest with coverage)
just docs-check     # scripts/check_docs_paths.py: module paths and relative links in the docs resolve
uv lock --project engine --check   # lockfile in sync with pyproject.toml
```

Notes:

- Tests run in random order (`pytest-randomly`) and must be offline and
  deterministic. Follow the existing patterns: `TestClient` as a context
  manager, `tmp_path`, `monkeypatch`. Do not skip or weaken existing tests;
  adapt them only when behaviour intentionally changes, and say so in the PR.
- mypy is strict for the core packages and incrementally stricter for the
  substrates (`[tool.mypy]` overrides in `engine/pyproject.toml`).
- Snapshots under `engine/tests/snapshots/` pin the CLI help, the OpenAPI
  document and the MCP tool schemas. A deliberate change regenerates them (the
  failing test prints how) and is called out in the CHANGELOG.
- Library code logs through `logging.getLogger(__name__)`; `print()` belongs
  to CLI modules only (ruff enforces `T20`).
- If you touch `engine/pyproject.toml`, run `uv lock --project engine` and
  commit `uv.lock`.
- Markdown docs: every backticked `knowledge_engine.*` path and every relative
  link must resolve (`just docs-check`); the CI `docs` job also runs an
  offline `lychee` link check. `01-user-docs/CONFIGURATION.md` is generated —
  edit `knowledge_engine/core/settings.py` and regenerate with
  `uv run --project engine python -m knowledge_engine.core.settings --markdown > 01-user-docs/CONFIGURATION.md`.

## Commits

We use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<scope>): <summary>

<body: what and why>
```

Types in use: `feat`, `fix`, `refactor`, `docs`, `test`, `build`, `ci`,
`chore`, `perf`. Scopes are package or area names (`mcp`, `server`, `board`,
`retrieval`, `cli`, `docs`, `deps`, …). Mark a breaking change with `!` after
the type/scope (`feat(mcp)!: …`) and describe the migration in the body and in
the CHANGELOG. Keep commits logical: one concern per commit, tests with the
code they cover.

## Pull requests

- Open an issue first for anything larger than a bug fix, so the design can be
  discussed before the code exists.
- Fill in the PR template: what changes and why, the type of change, and the
  checklist (`just check` passes, tests added, CHANGELOG and docs updated).
- Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for every
  user-visible change (Keep a Changelog categories: Added / Changed /
  Deprecated / Removed / Fixed / Security; breaking changes under
  **Breaking** with a migration note).
- Update the docs that describe the behaviour you change (paths, env vars,
  commands, routes). `01-user-docs/` is for operators and users,
  `02-dev-docs/` for engine internals.
- PRs are squash-merged with the PR title as the commit subject, so the title
  must itself be a Conventional Commit.

### Stacked PRs (the "train")

Large changes are delivered as an ordered series of pull requests, each
building on the previous one (the 3.0 release was delivered as a train of
fourteen PRs). Rules for a train:

1. Each PR is reviewable on its own: one theme, its own tests, its own
   CHANGELOG entry, and a green gate on its branch.
2. Branch each PR from the tip of the previous one (or from `main` once that
   one has merged); rebase, do not merge `main` into the branch.
3. Merge in order. When a later PR needs something from an earlier one that is
   still open, say so in the description.
4. The gate is run locally on the final tree of every PR before it is marked
   ready — GitHub's merge queue is not available on this repository, so the
   local gate plus required CI checks on the PR are what protect `main`
   (see [ADR 0004](02-dev-docs/adr/0004-merge-train.md)).

## Compatibility and deprecation policy

- Semantic versioning applies to the engine package, the MCP tool surface
  (names, parameters, result shapes) and the registry schema.
- When a module or public name moves, keep a compatibility shim at the old
  location that re-exports the new one and emits a `DeprecationWarning`.
  Shims live for **one major version**: added in `N.x`, removed in `(N+1).0`
  (`knowledge_engine.mcp_server`, deprecated in 3.0, will be removed in 4.0).
- Behaviour changes that an operator can observe (defaults, env vars, exit
  codes, response fields) are **Changed** or **Breaking** in the CHANGELOG,
  never silent.
- Environment variables are declared on `knowledge_engine.core.settings.Settings`;
  add new ones there (with a description and a `read_by` module) so they
  appear in `config show` and the generated reference.

## Security issues

Do not open a public issue for a suspected vulnerability. Use the private
channel described in [SECURITY.md](SECURITY.md).

## Releases

Maintainers cut releases as described in
[02-dev-docs/RELEASING.md](02-dev-docs/RELEASING.md).
