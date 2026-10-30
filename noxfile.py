"""Nox sessions for knowledge-engine: the CI checks plus the developer services.

Run with nox on the uv backend (no global install needed)::

    uvx nox -l                    # list sessions
    uvx nox                       # default: lint, typecheck, tests (3.12-3.14), docs
    uvx nox -s tests-3.13 -- -k broker   # arguments after -- go to pytest
    uvx nox -s ui build lock      # the remaining CI jobs

Developer services (not run by default)::

    uvx nox -s self_index -- --query "nested entries"   # index the repo with Ollama + bge-m3
    uvx nox -s serena                                   # Serena MCP server (stdio) for this repo
    uvx nox -s serena -- --check                        # start Serena once and run symbol queries
    uvx nox -s observability -- up                      # Loki + Alloy + Grafana (docker compose)

Each session installs the locked dependency groups from engine/uv.lock into its
own virtual environment (``uv sync --locked``), so results match CI. See
02-dev-docs/DEV-TOOLING.md.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import nox

nox.needs_version = ">=2025.2.9"
nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True
nox.options.error_on_missing_interpreters = True
nox.options.sessions = ["lint", "typecheck", "tests", "docs"]

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
PYTHONS = ["3.12", "3.13", "3.14"]
#: Serena release the development setup is tested with (GPL-3.0-or-later; never bundled).
SERENA_SPEC = "serena-agent==2.1.0"
COMPOSE_FILE = ROOT / "deploy" / "observability" / "compose.yaml"


def sync(session: nox.Session, *groups: str, dev: bool = False) -> None:
    """Install the locked project plus ``groups`` into the session's virtualenv."""
    args = ["uv", "sync", "--locked", "--project", str(ENGINE)]
    if not dev:
        args.append("--no-dev")
    for group in groups:
        args += ["--group", group]
    session.run_install(
        *args,
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
        external=True,
    )


@nox.session(python=PYTHONS[0])
def lint(session: nox.Session) -> None:
    """ruff format check and ruff lint (what the CI lint job runs)."""
    sync(session, "lint")
    session.chdir(ENGINE)
    session.run("ruff", "format", "--check", "--diff", ".")
    session.run("ruff", "check", ".")


@nox.session(python=PYTHONS[0])
def typecheck(session: nox.Session) -> None:
    """mypy (strict) and registry validation."""
    sync(session, "lint")
    session.chdir(ENGINE)
    session.run("mypy")
    session.run("knowledge-engine", "registry", "validate", "../corpus/registry.json")


@nox.session(python=PYTHONS)
def tests(session: nox.Session) -> None:
    """pytest in random order with coverage; extra arguments go to pytest."""
    sync(session, "test")
    session.chdir(ENGINE)
    args = session.posargs or ["--cov", "--cov-report=term"]
    session.run("pytest", "-q", "-p", "randomly", *args)


@nox.session(python=PYTHONS[0])
def ui(session: nox.Session) -> None:
    """Dashboard browser smoke test (needs a Playwright Chromium)."""
    sync(session, "test", "ui")
    session.chdir(ENGINE)
    env = {"KE_UI_TESTS": "1"}
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path("/opt/pw-browsers").is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    session.run(
        "pytest", "-m", "ui", "tests/ui", "-p", "no:randomly", "-rs", *session.posargs, env=env
    )


@nox.session(python=PYTHONS[0])
def docs(session: nox.Session) -> None:
    """Docs paths/links resolve and the generated CONFIGURATION.md is current."""
    sync(session)
    session.run("python", "scripts/check_docs_paths.py")
    reference = session.run(
        "python",
        "-m",
        "knowledge_engine.core.settings",
        "--markdown",
        silent=True,
        env=_clean_ke_env(),
    )
    committed = (ROOT / "01-user-docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    if reference != committed:
        session.error(
            "01-user-docs/CONFIGURATION.md is stale; regenerate it with "
            "`python -m knowledge_engine.core.settings --markdown > 01-user-docs/CONFIGURATION.md`"
        )


@nox.session(python=PYTHONS[0])
def build(session: nox.Session) -> None:
    """sdist + wheel, twine check, and a clean-venv install smoke test."""
    sync(session, "build")
    dist = Path(session.create_tmp()) / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    session.run("uv", "build", "--project", str(ENGINE), "--out-dir", str(dist), external=True)
    artifacts = sorted(str(p) for p in dist.iterdir() if p.name.endswith((".whl", ".tar.gz")))
    session.run("twine", "check", *artifacts)
    wheel = next(p for p in artifacts if p.endswith(".whl"))
    smoke = Path(session.create_tmp()) / "smoke"
    session.run("uv", "venv", "-q", "--clear", "--python", PYTHONS[0], str(smoke), external=True)
    python = smoke / ("Scripts" if os.name == "nt" else "bin") / "python"
    session.run("uv", "pip", "install", "-q", "--python", str(python), wheel, external=True)
    session.run(
        str(python),
        "-c",
        "import importlib.metadata as m, knowledge_engine;"
        "from knowledge_engine.server.app import create_app;"
        "assert knowledge_engine.__version__ == m.version('knowledge-engine');"
        "create_app(); print('smoke ok', knowledge_engine.__version__)",
        external=True,
    )


@nox.session(venv_backend="none")
def lock(session: nox.Session) -> None:
    """engine/uv.lock is in sync with engine/pyproject.toml."""
    session.run("uv", "lock", "--check", "--project", str(ENGINE), external=True)


@nox.session(python=PYTHONS[0], default=False)
def self_index(session: nox.Session) -> None:
    """Index the repository's docs with a local model (scripts/index_repo.py)."""
    sync(session)
    session.run("python", "scripts/index_repo.py", *session.posargs)


@nox.session(python=PYTHONS[0], default=False)
def serena(session: nox.Session) -> None:
    """Serena MCP server over stdio for this checkout; `-- --check` runs a health check."""
    sync(session)
    # Serena lives in its own environment: it is GPL-licensed and has its own pins.
    serena_bin = _serena_command(session)
    env = {"SERENA_USAGE_REPORTING": "false"}
    if shutil.which("uvx") is None:
        session.warn("uvx is not on PATH; Serena starts the pyright language server through it")
    if "--check" in session.posargs:
        session.run("python", "scripts/serena_check.py", "--serena", serena_bin, env=env)
        return
    session.run(
        *serena_bin.split(),
        "start-mcp-server",
        "--project",
        str(ROOT),
        "--transport",
        "stdio",
        "--context",
        "agent",
        "--agent-interface",
        "tools",
        "--enable-web-dashboard",
        "false",
        "--open-web-dashboard",
        "false",
        "--enable-gui-log-window",
        "false",
        *[a for a in session.posargs if a != "--check"],
        env=env,
        external=True,
    )


@nox.session(venv_backend="none", default=False)
def observability(session: nox.Session) -> None:
    """docker compose for deploy/observability; arguments go to compose (default: config -q)."""
    args = session.posargs or ["config", "-q"]
    session.run("docker", "compose", "-f", str(COMPOSE_FILE), *args, external=True)


def _serena_command(session: nox.Session) -> str:
    """A Serena executable: $SERENA, else one installed into a cached tool venv."""
    if os.environ.get("SERENA"):
        return os.environ["SERENA"]
    tool_env = ROOT / ".nox" / "_serena"
    exe = tool_env / ("Scripts" if os.name == "nt" else "bin") / "serena"
    if not exe.exists():
        session.run("uv", "venv", "-q", "--python", "3.12", str(tool_env), external=True)
        session.run(
            "uv",
            "pip",
            "install",
            "-q",
            "--python",
            str(exe.parent / "python"),
            SERENA_SPEC,
            external=True,
        )
    return str(exe)


def _clean_ke_env() -> dict[str, str | None]:
    """Unset every KE_*/OLLAMA_* variable so the generated reference shows defaults."""
    names = [k for k in os.environ if k.startswith(("KE_", "OLLAMA_", "BOARD_"))]
    return dict.fromkeys(names)
