# Releasing

How a maintainer cuts a release. Releases are GitHub Releases on `vX.Y.Z`
tags; the package is **not** published to PyPI by default (see the last
section).

## Prerequisites

- `main` is green (CI: lint, test matrix, build, zizmor, docs).
- Every user-visible change since the last release has a line under
  `## [Unreleased]` in `CHANGELOG.md`.
- You can push tags to `ARC-907/knowledge-engine`.

## Steps

1. **Pick the version.** Semantic versioning over the engine package, the MCP
   tool surface and the registry schema: breaking → major, features → minor,
   fixes only → patch. Pre-releases use PEP 440 suffixes (`3.1.0rc1`); the
   release workflow marks tags containing `a`, `b` or `rc` as pre-releases.

2. **Bump the version** in `engine/pyproject.toml` and refresh the lockfile
   (the lock records the project version, so `uv lock --check` fails
   otherwise):

   ```bash
   uv version --project engine 3.1.0
   uv lock --project engine
   ```

3. **Update `CHANGELOG.md`.** Rename `## [Unreleased]` to
   `## [3.1.0] — YYYY-MM-DD`, add a fresh empty `## [Unreleased]` above it, and
   add the compare link at the bottom:

   ```
   [Unreleased]: https://github.com/ARC-907/knowledge-engine/compare/v3.1.0...HEAD
   [3.1.0]: https://github.com/ARC-907/knowledge-engine/compare/v3.0.0...v3.1.0
   ```

   For a major version, include a **Migrating from N.x** subsection. The
   release workflow extracts everything between `## [3.1.0]` and the next
   `## [` heading as the release notes, so the section must be complete and
   self-contained.

4. **Update version references** that are not derived from the package:
   the README install command (`@v3.1.0`), `scripts/mcp-client-config.example.json`
   and `01-user-docs/MCP-WIRING.md` (`uvx --from …@v3.1.0…`), and the
   supported-versions table in `SECURITY.md` when a new major starts.

5. **Run the gate** on the final tree:

   ```bash
   just check && just docs-check && uv lock --project engine --check
   ```

6. **Commit and tag.** One commit, then an annotated tag whose name is `v` +
   the exact package version (the workflow refuses a mismatch):

   ```bash
   git commit -am "chore(release): 3.1.0"
   git tag -a v3.1.0 -m "knowledge-engine 3.1.0"
   git push origin main v3.1.0
   ```

7. **Watch the release workflow** (`.github/workflows/release.yml`, triggered
   by the tag push). It:
   - checks that the tag matches `engine/pyproject.toml`;
   - builds the sdist and wheel with `uv build` and verifies the metadata
     with `twine check`;
   - attests build provenance (Sigstore via `actions/attest-build-provenance`);
   - extracts the CHANGELOG section for the version and creates the GitHub
     Release with the artifacts attached (`gh release create`, with
     auto-generated notes appended).

8. **Verify the release.** Install from the tag into a clean environment and
   run the smoke checks:

   ```bash
   uv tool install --force "git+https://github.com/ARC-907/knowledge-engine@v3.1.0#subdirectory=engine"
   knowledge-engine --help
   knowledge-engine mcp --selftest
   ```

   Then confirm the release page shows the two artifacts and the provenance
   attestation.

9. **Announce** as appropriate and close the milestone, if one was used.

## Fixing a bad release

Never move a tag. Fix forward: bump to the next patch version, repeat the steps
above, and mark the broken GitHub Release as such in its notes (or delete it if
nothing has installed from it).

## PyPI (disabled by default)

The name `knowledge-engine` on PyPI belongs to an unrelated project, so the
`pypi` job in `release.yml` runs only when the repository variable
`PYPI_PUBLISH` is set to `true`. Enabling it requires:

1. Choosing a distribution name that is free on PyPI and setting it as
   `[project].name` in `engine/pyproject.toml` (the import package and the
   console script keep their names).
2. Configuring a PyPI **trusted publisher** for this repository, workflow
   `release.yml`, environment `pypi` (the job uses OIDC; no API token is
   stored).
3. Creating the `pypi` environment in the repository settings (optionally with
   required reviewers) and setting `PYPI_PUBLISH=true` under *Settings →
   Secrets and variables → Actions → Variables*.

Until then, installation is from GitHub: `uv tool install
"git+https://github.com/ARC-907/knowledge-engine@vX.Y.Z#subdirectory=engine"`
or the wheel attached to the release.
