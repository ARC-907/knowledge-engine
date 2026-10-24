# ADR 0004 — Deliver large changes as a merge train of stacked PRs with a local gate

- **Status:** Accepted (3.0.0)
- **Date:** 2026-10

## Context

The 3.0 modernisation touched every part of the repository: build system,
CI, paths, security defaults, the MCP server, the HTTP layer, retrieval,
substrates, dashboard, containers and docs. One pull request would have been
unreviewable; fully independent PRs would have conflicted constantly, because
later work depended on earlier refactors (typed settings, the path resolver,
the CLI package). The repository is a personal GitHub repository, where
GitHub's merge queue is not available, and `main` has no team of reviewers to
absorb a long-lived integration branch.

## Decision

Deliver the work as an ordered **merge train** of stacked pull requests
(3.0 was delivered as fourteen of them), with these rules:

- Each PR has one theme, its own tests, its own CHANGELOG snippet and a green
  gate on its own branch. A PR may rely on the previous PR but never on a
  later one.
- PRs are rebased onto the tip of their predecessor (or onto `main` once it
  merged) rather than merging `main` in, so the final history is linear and
  each squash-merged commit is a Conventional Commit.
- The **gate** is run locally on the final tree of every PR before it is
  marked ready: `ruff format --check`, `ruff check`, `mypy`, `pytest` (twice,
  random order), `uv lock --check`, and the docs checker. CI runs the same
  checks on the PR and on `main` after merge, with the Linux test job required.
- Merges happen in train order. Cross-PR fixes discovered during integration
  are applied to the PR that owns the code, not patched into a later one.
- A final docs PR refreshes every guide against the integrated tree and
  compiles the CHANGELOG from the per-PR snippets.

## Consequences

- Reviewers see focused diffs with a stated dependency, and `git bisect` works
  over the merged history.
- The local gate substitutes for the merge queue: it is the only thing that
  tests the *combination* of a PR with everything merged before it, so it must
  be run on the rebased tree, not on the original branch.
- Early PRs may ship documentation or config that later PRs change again
  (for example the dashboard moving into the package before the compiled
  stylesheet landed); the CHANGELOG records the release's net effect, not the
  intermediate states.
- The approach is documented in `CONTRIBUTING.md` so future large changes
  follow the same shape.
