# Task runner for knowledge-engine. Install `just` (https://just.systems) or copy the
# commands below; everything is plain `uv`.

set shell := ["bash", "-euo", "pipefail", "-c"]

engine := "engine"
uv := "uv --project " + engine

# List recipes
default:
    @just --list

# Create/refresh the project virtualenv with all dependency groups
install:
    {{uv}} sync --locked --all-groups

# Lint + format check (what CI runs)
lint:
    {{uv}} run ruff format --check .
    {{uv}} run ruff check .

# Auto-fix lint and apply formatting
fmt:
    {{uv}} run ruff check --fix .
    {{uv}} run ruff format .

# Static type check
typecheck:
    {{uv}} run mypy

# Full test suite with coverage
test *args:
    {{uv}} run pytest -q --cov --cov-report=term-missing {{args}}

# Fast test loop (no coverage, randomized order, parallel)
test-fast *args:
    {{uv}} run pytest -q -n auto -p randomly {{args}}

# Everything a PR must pass
check: lint typecheck test

# Build sdist + wheel into engine/dist and verify metadata
build:
    rm -rf {{engine}}/dist
    {{uv}} build
    {{uv}} run twine check {{engine}}/dist/*

# Refresh the lockfile after editing pyproject.toml
lock:
    {{uv}} lock

# Run the HTTP API + dashboard locally
serve *args:
    {{uv}} run knowledge-engine serve {{args}}

# Run the MCP stdio server
mcp *args:
    {{uv}} run knowledge-engine mcp {{args}}

# Install git hooks
hooks:
    {{uv}} run pre-commit install

# Rebuild the dashboard stylesheet (needs Node 22+; output is committed)
ui-css:
    npm --prefix ui-src install --no-audit --no-fund
    npm --prefix ui-src run build:css

# Browser smoke test of the dashboard (needs the `ui` group + a Playwright Chromium)
ui-test *args:
    cd {{engine}} && KE_UI_TESTS=1 uv run --group ui pytest -q -m ui tests/ui {{args}}

# Re-download and SHA-384-verify vendored dashboard assets (Alpine.js)
vendor:
    {{uv}} run python scripts/vendor.py

# Verify the committed vendored assets match their pinned digests (offline)
vendor-check:
    {{uv}} run python scripts/vendor.py --check
