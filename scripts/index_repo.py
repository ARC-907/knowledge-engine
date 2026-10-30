"""Index this repository's documentation with the engine itself.

Builds a self-contained engine home (default ``.knowledge-engine/self-index``,
git-ignored) whose registry points at the repository's Markdown: the user and
developer docs, the ADRs, the deployment notes, the sample corpus and the
top-level project files. It then rebuilds the keyword index and the embedding
index with a local model, so the repository can be searched in ``fts``,
``vector`` and ``hybrid`` mode, served over HTTP, or exposed to an MCP client.

Code symbols are not indexed here; the Serena session (``nox -s serena``)
answers symbol questions over the same checkout.

Usage (from the repository root)::

    uv --project engine run python scripts/index_repo.py              # Ollama + bge-m3
    uv --project engine run python scripts/index_repo.py --provider fake  # offline dry run
    uv --project engine run python scripts/index_repo.py --query "nested entry ownership"
    uv --project engine run python scripts/index_repo.py --print-env  # env for serve/mcp

The embedding step needs Ollama at ``--ollama-url`` (default ``KE_OLLAMA_URL``
or ``http://127.0.0.1:11434``) with the model pulled (``ollama pull bge-m3``).
See 02-dev-docs/DEV-TOOLING.md ("Repository self-index").
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = Path(".knowledge-engine") / "self-index"
DEFAULT_MODEL = "bge-m3"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

#: (id, name, path relative to the repository root, tags, description)
ENTRIES: tuple[tuple[str, str, str, list[str], str], ...] = (
    (
        "repo-user-docs",
        "User documentation",
        "01-user-docs",
        ["docs", "user"],
        "Operator and user guides.",
    ),
    (
        "repo-dev-docs",
        "Developer documentation",
        "02-dev-docs",
        ["docs", "dev"],
        "Architecture, roadmap, design, bug and development logs.",
    ),
    (
        "repo-adr",
        "Architecture decision records",
        "02-dev-docs/adr",
        ["docs", "adr"],
        "One file per decision; nested inside the developer docs.",
    ),
    (
        "repo-deploy",
        "Deployment",
        "deploy",
        ["deploy"],
        "Observability stack and deployment notes.",
    ),
    (
        "repo-samples",
        "Sample corpus",
        "corpus/samples",
        ["sample"],
        "The demo library and skill shipped with the engine.",
    ),
    ("repo-readme", "README", "README.md", ["project"], "Project overview."),
    ("repo-changelog", "CHANGELOG", "CHANGELOG.md", ["project"], "Release notes."),
    (
        "repo-contributing",
        "CONTRIBUTING",
        "CONTRIBUTING.md",
        ["project"],
        "How to build, test and contribute.",
    ),
    ("repo-security", "SECURITY", "SECURITY.md", ["project"], "Security policy."),
)

_logger = logging.getLogger("index_repo")
# Runs the CLI with the interpreter running this script (the engine's venv).
_CLI = "import sys; from knowledge_engine.cli import main; sys.exit(main(sys.argv[1:]))"


def build_registry(root: Path) -> dict[str, Any]:
    """Registry document for the entries that exist under ``root``."""
    libraries = []
    for entry_id, name, rel, tags, description in ENTRIES:
        if not (root / rel).exists():
            _logger.warning("skipping %s: %s does not exist", entry_id, rel)
            continue
        libraries.append(
            {
                "id": entry_id,
                "kind": "library",
                "name": name,
                "path": rel,
                "enabled": True,
                "auto_registered": False,
                "description": description,
                "tags": tags,
            }
        )
    return {
        "version": 1,
        "corpus_root": "./",
        "libraries": libraries,
        "skills": [],
        "kits": [],
        "tools": [],
        "lifecycle": {
            "watch_enabled": False,
            "schedule": None,
            "auto_register_new_folders": False,
            "on_toggle_reindex": "next-cycle",
        },
    }


def engine_env(
    root: Path, out: Path, *, provider: str, model: str, ollama_url: str
) -> dict[str, str]:
    """Environment that points the engine at the self-index home."""
    return {
        "KE_HOME": str(out),
        "KE_CORPUS_ROOT": str(root),
        "KE_REGISTRY_PATH": str(out / "registry.json"),
        "KE_DATA_DIR": str(out / "data"),
        "KE_EMBED_PROVIDER": provider,
        "KE_EMBED_MODEL": model,
        "KE_OLLAMA_URL": ollama_url,
    }


def ollama_has_model(url: str, model: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Whether Ollama at ``url`` answers and lists ``model`` (``name`` or ``name:tag``)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url.rstrip('/')}/api/tags", timeout=timeout) as resp:
            tags = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Ollama at {url} is not reachable: {exc}"
    names = {m.get("name", "") for m in tags.get("models", [])}
    wanted = {model, f"{model}:latest"} if ":" not in model else {model}
    if names & wanted:
        return True, ""
    return False, f"Ollama at {url} does not have {model!r} (pull it: ollama pull {model})"


def run_engine(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-c", _CLI, *args]
    _logger.info("$ knowledge-engine %s", shlex.join(args))
    proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        cmd, env={**os.environ, **env}, text=True, capture_output=True, check=False
    )
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"knowledge-engine {args[0]} failed with exit code {proc.returncode}")
    return proc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument(
        "--out", type=Path, default=None, help=f"engine home (default: <root>/{DEFAULT_OUT})"
    )
    parser.add_argument("--provider", choices=("ollama", "fake"), default="ollama")
    parser.add_argument("--model", default=os.environ.get("KE_EMBED_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=os.environ.get("KE_OLLAMA_URL", DEFAULT_OLLAMA_URL))
    parser.add_argument(
        "--batch",
        type=int,
        default=int(os.environ.get("KE_EMBED_BATCH", "8")),
        help="chunks per embedding request (default 8: a CPU-only Ollama embeds bge-m3 "
        "at roughly 1-2 s per chunk, and each request must finish within 60 s)",
    )
    parser.add_argument("--rebuild", action="store_true", help="drop the embedding index first")
    parser.add_argument(
        "--skip-embeddings", action="store_true", help="keyword index only (no model needed)"
    )
    parser.add_argument(
        "--query", action="append", default=[], help="run a search afterwards (repeatable)"
    )
    parser.add_argument("--mode", choices=("fts", "vector", "hybrid"), default="hybrid")
    parser.add_argument(
        "--print-env", action="store_true", help="print the environment for serve/mcp and exit"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    root = args.root.resolve()
    out = (args.out or root / DEFAULT_OUT).resolve()
    env = engine_env(
        root, out, provider=args.provider, model=args.model, ollama_url=args.ollama_url
    )
    if args.print_env:
        for key, value in env.items():
            print(f"export {key}={shlex.quote(value)}")
        return 0

    (out / "data").mkdir(parents=True, exist_ok=True)
    registry = build_registry(root)
    (out / "registry.json").write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    _logger.info("registry: %d entries -> %s", len(registry["libraries"]), out / "registry.json")

    run_engine(["registry", "validate", str(out / "registry.json")], env)
    run_engine(["reindex"], env)

    embeddings = not args.skip_embeddings
    if embeddings and args.provider == "ollama":
        ok, reason = ollama_has_model(args.ollama_url, args.model)
        if not ok:
            _logger.error("%s; rerun with --provider fake or --skip-embeddings", reason)
            return 2
    if embeddings:
        build = ["embeddings", "build", "--provider", args.provider, "--model", args.model]
        build += ["--batch", str(max(1, args.batch))]
        build.append("--rebuild" if args.rebuild else "--incremental")
        run_engine(build, env)
        run_engine(["embeddings", "status"], env)

    mode = args.mode if embeddings else "fts"
    for query in args.query:
        print(f"\n# {mode}: {query}")
        run_engine(["search", "--mode", mode, "--limit", "5", query], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
