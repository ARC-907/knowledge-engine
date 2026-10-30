"""The read-only tool mapping: allowlist, engine-side schemas, result normalization.

Only three upstream tools are ever forwarded:

==========================  =====================================  ==================
Engine tool                 Upstream Serena tool                   Effect
==========================  =====================================  ==================
``serena.find_symbol``      ``find_symbol``                        read
``serena.get_symbols_overview``  ``get_symbols_overview``          read
``serena.find_referencing_symbols``  ``find_referencing_symbols``  read
==========================  =====================================  ==================

Everything else -- shell execution, file creation, every editing, renaming
and deleting tool, memories, onboarding, ``activate_project`` (routing is
done by one process per project), the REPL tool and any tool a future
Serena adds -- is rejected by :func:`upstream_tool` before a session is even
looked up.

The engine schemas (:class:`FindSymbolArgs`, ...) are the engine's own: a
``project_id`` plus typed, bounded arguments; only the documented upstream
argument names are forwarded. Every ``relative_path`` going in or coming out
is resolved against the project root and must stay inside it; results that
point outside are dropped and reported in ``rejected_paths``.

Line numbers: Serena's ``body_location`` is 0-based and inclusive; code
locators (ADR 0007) are 1-based with an exclusive end, so ``start_line + 1``
and ``end_line + 2``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..knowledge.contracts import (
    OPAQUE_ID_PATTERN,
    CodeLocator,
    UnsupportedLocator,
    validation_messages,
)
from ..knowledge.errors import InvalidInputError, OutputInvalidError
from .repo import PathEscapeError, RepoState, relative_path

_logger = logging.getLogger(__name__)

ENGINE_PREFIX = "serena."
#: Upstream tools the adapter forwards (read-only); nothing else is ever called.
ALLOWED_TOOLS: tuple[str, ...] = (
    "find_symbol",
    "get_symbols_overview",
    "find_referencing_symbols",
)
#: Most entries kept from one upstream answer.
MAX_RESULTS = 500


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=OPAQUE_ID_PATTERN, description="Configured project id.")


class FindSymbolArgs(_Args):
    """Find symbols by name path (``Class/method``), optionally inside a file or directory."""

    name_path_pattern: str = Field(min_length=1, max_length=500)
    relative_path: str = Field(default="", max_length=1024)
    substring_matching: bool = False
    include_body: bool = False
    depth: int = Field(default=0, ge=0, le=5)
    max_matches: int = Field(default=50, ge=1, le=MAX_RESULTS)


class SymbolsOverviewArgs(_Args):
    """Top-level symbols of one file or directory, grouped by kind."""

    relative_path: str = Field(min_length=1, max_length=1024)
    depth: int = Field(default=0, ge=0, le=3)


class ReferencingSymbolsArgs(_Args):
    """Symbols that reference the symbol ``name_path`` defined in ``relative_path``."""

    name_path: str = Field(min_length=1, max_length=500)
    relative_path: str = Field(min_length=1, max_length=1024)
    max_results: int = Field(default=100, ge=1, le=MAX_RESULTS)


ENGINE_TOOLS: dict[str, tuple[str, type[_Args]]] = {
    f"{ENGINE_PREFIX}find_symbol": ("find_symbol", FindSymbolArgs),
    f"{ENGINE_PREFIX}get_symbols_overview": ("get_symbols_overview", SymbolsOverviewArgs),
    f"{ENGINE_PREFIX}find_referencing_symbols": (
        "find_referencing_symbols",
        ReferencingSymbolsArgs,
    ),
}


def upstream_tool(name: str) -> str:
    """The upstream tool an engine tool (or upstream name) maps to; anything else is refused."""
    mapped = ENGINE_TOOLS.get(name)
    upstream = mapped[0] if mapped is not None else name
    if upstream not in ALLOWED_TOOLS:
        _logger.warning("refusing to forward Serena tool %r (not in the read-only allowlist)", name)
        raise InvalidInputError(
            f"Serena tool {name!r} is not allowed; the adapter is read-only",
            details={"allowed": [f"{ENGINE_PREFIX}{t}" for t in ALLOWED_TOOLS]},
        )
    return upstream


def parse_args(tool: str, arguments: Mapping[str, Any]) -> _Args:
    """Validate ``arguments`` against the engine schema of ``tool``."""
    mapped = ENGINE_TOOLS.get(tool)
    if mapped is None:
        upstream_tool(tool)  # raises with the allowlist
        raise InvalidInputError(f"unknown Serena tool {tool!r}")
    try:
        return mapped[1].model_validate(dict(arguments))
    except ValidationError as exc:
        raise InvalidInputError(
            f"invalid arguments for {tool}", details={"errors": validation_messages(exc)}
        ) from exc


def upstream_arguments(args: _Args, root: Path) -> dict[str, Any]:
    """The upstream call arguments (documented names only; paths checked against ``root``)."""
    if isinstance(args, FindSymbolArgs):
        out: dict[str, Any] = {
            "name_path_pattern": args.name_path_pattern,
            "substring_matching": args.substring_matching,
            "include_body": args.include_body,
            "depth": args.depth,
            "max_matches": args.max_matches,
        }
        if args.relative_path:
            out["relative_path"] = _checked(root, args.relative_path)
        return out
    if isinstance(args, SymbolsOverviewArgs):
        return {"relative_path": _checked(root, args.relative_path), "depth": args.depth}
    if isinstance(args, ReferencingSymbolsArgs):
        return {
            "name_path": args.name_path,
            "relative_path": _checked(root, args.relative_path),
        }
    raise InvalidInputError("unsupported Serena arguments")


def _checked(root: Path, path: str) -> str:
    try:
        return relative_path(root, path)
    except PathEscapeError as exc:
        raise InvalidInputError(
            f"relative_path must stay inside the project: {exc}",
            details={"field": "relative_path"},
        ) from exc


# ── record identity ──────────────────────────────────────────────────

_RECORD_PREFIX = "f."
_HASH_PREFIX = "h."
_MAX_ENCODED = 190


def record_id_for(path: str) -> str:
    """The record id of a repository file: ``f.<base32(path)>``, or ``h.<sha>`` when too long."""
    encoded = base64.b32encode(path.encode("utf-8")).decode("ascii").rstrip("=").lower()
    if len(encoded) + len(_RECORD_PREFIX) <= _MAX_ENCODED:
        return _RECORD_PREFIX + encoded
    return _HASH_PREFIX + hashlib.sha256(path.encode("utf-8")).hexdigest()[:40]


def path_for(record_id: str) -> str | None:
    """The path a ``f.`` record id encodes (``None`` for hashed or foreign ids)."""
    if not record_id.startswith(_RECORD_PREFIX):
        return None
    body = record_id[len(_RECORD_PREFIX) :].upper()
    try:
        return base64.b32decode(body + "=" * (-len(body) % 8)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


# ── normalization ────────────────────────────────────────────────────


def _lines(location: Any) -> tuple[int, int] | None:
    """Serena's 0-based inclusive ``body_location`` as 1-based ``[start, end)``."""
    if not isinstance(location, Mapping):
        return None
    start, end = location.get("start_line"), location.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        return None
    return start + 1, end + 2


def code_locator(
    repository_id: str,
    state: RepoState,
    path: str,
    *,
    symbol: str | None,
    lines: tuple[int, int] | None,
) -> CodeLocator | UnsupportedLocator:
    """A code locator pinned to the repository state (or why none can be given)."""
    if not state.usable:
        return UnsupportedLocator(reason=state.reason or "the repository version is unknown")
    return CodeLocator(
        repository_id=repository_id,
        commit=state.commit,
        snapshot_digest=state.snapshot_digest,
        path=path,
        symbol=symbol[:500] if symbol else None,
        start_line=lines[0] if lines else None,
        end_line=lines[1] if lines else None,
    )


class Normalized(BaseModel):
    """An engine tool result: entries, the repository version and anything rejected."""

    tool: str
    project_id: str
    repository: dict[str, Any]
    results: Any
    rejected_paths: list[str] = Field(default_factory=list)
    truncated: bool = False


def _repository(repository_id: str, state: RepoState) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "commit": state.commit,
        "snapshot_digest": state.snapshot_digest,
        "dirty": state.snapshot_digest is not None,
        "reason": state.reason,
    }


def _symbol_entry(
    item: Any,
    *,
    root: Path,
    repository_id: str,
    state: RepoState,
    rejected: list[str],
    path_key: str = "relative_path",
    path: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    raw_path = path if path is not None else item.get(path_key)
    try:
        rel = relative_path(root, str(raw_path))
    except PathEscapeError:
        rejected.append(str(raw_path)[:300])
        return None
    name_path = item.get("name_path")
    lines = _lines(item.get("body_location"))
    entry: dict[str, Any] = {
        "name_path": str(name_path) if isinstance(name_path, str) else None,
        "kind": str(item.get("kind")) if item.get("kind") is not None else None,
        "path": rel,
        "record_id": record_id_for(rel),
        "locator": code_locator(
            repository_id,
            state,
            rel,
            symbol=entry_symbol(name_path),
            lines=lines,
        ).model_dump(mode="json"),
    }
    if isinstance(item.get("body"), str):
        entry["body"] = item["body"]
    if isinstance(item.get("content_around_reference"), str):
        entry["context"] = item["content_around_reference"][:2000]
    return entry


def entry_symbol(name_path: Any) -> str | None:
    """Serena's ``Class/method`` name path as a qualified ``Class.method`` symbol."""
    return name_path.replace("/", ".") if isinstance(name_path, str) and name_path else None


def normalize(
    tool: str,
    args: _Args,
    payload: Any,
    *,
    root: Path,
    repository_id: str,
    state: RepoState,
) -> Normalized:
    """Turn an upstream answer into the engine result (paths contained, locators pinned)."""
    rejected: list[str] = []
    truncated = False

    def entry_of(item: Any, path: str | None = None) -> dict[str, Any] | None:
        return _symbol_entry(
            item,
            root=root,
            repository_id=repository_id,
            state=state,
            rejected=rejected,
            path=path,
        )

    results: Any
    if tool == "find_symbol":
        if not isinstance(payload, list):
            raise OutputInvalidError("Serena's find_symbol answer is not a list")
        results = [e for e in (entry_of(item) for item in payload[:MAX_RESULTS]) if e]
        truncated = len(payload) > MAX_RESULTS
    elif tool == "get_symbols_overview":
        if not isinstance(payload, Mapping):
            raise OutputInvalidError("Serena's get_symbols_overview answer is not an object")
        results = {
            str(kind): [_overview_name(n) for n in names if _overview_name(n)][:MAX_RESULTS]
            for kind, names in payload.items()
            if isinstance(names, list)
        }
    elif tool == "find_referencing_symbols":
        if not isinstance(payload, Mapping):
            raise OutputInvalidError("Serena's find_referencing_symbols answer is not an object")
        limit = args.max_results if isinstance(args, ReferencingSymbolsArgs) else MAX_RESULTS
        flat: list[dict[str, Any]] = []
        for path, kinds in payload.items():
            if not isinstance(kinds, Mapping):
                continue
            for kind, items in kinds.items():
                for item in items if isinstance(items, list) else []:
                    entry = entry_of({**item, "kind": kind}, path=str(path))
                    if entry is not None:
                        flat.append(entry)
        flat.sort(key=lambda e: (e["path"], e["locator"].get("start_line") or 0))
        truncated = len(flat) > limit
        results = flat[:limit]
    else:  # pragma: no cover - guarded by upstream_tool
        raise InvalidInputError(f"unsupported tool {tool!r}")
    if rejected:
        _logger.warning(
            "Serena returned %d path(s) outside project %s; dropped",
            len(rejected),
            args.project_id,
        )
    return Normalized(
        tool=f"{ENGINE_PREFIX}{tool}",
        project_id=args.project_id,
        repository=_repository(repository_id, state),
        results=results,
        rejected_paths=sorted(set(rejected)),
        truncated=truncated,
    )


def _overview_name(item: Any) -> str | None:
    if isinstance(item, str):
        return item[:500]
    if isinstance(item, Mapping) and isinstance(item.get("name_path") or item.get("name"), str):
        return str(item.get("name_path") or item.get("name"))[:500]
    return None


def tool_definitions() -> list[dict[str, Any]]:
    """``{"name", "description", "inputSchema"}`` of the engine's Serena tools."""
    descriptions: dict[str, str] = {
        "find_symbol": "Find code symbols by name path (e.g. `Class/method`) in a configured "
        "project's repository via Serena (read-only). Each result carries a code locator "
        "pinned to the commit, or to a snapshot digest when the working tree is dirty.",
        "get_symbols_overview": "List the top-level symbols of a file or directory of a "
        "configured project, grouped by kind (read-only, via Serena).",
        "find_referencing_symbols": "Find the symbols that reference a symbol (by name path "
        "and defining file) in a configured project (read-only, via Serena).",
    }
    return [
        {
            "name": name,
            "description": descriptions[upstream],
            "inputSchema": model.model_json_schema(),
        }
        for name, (upstream, model) in ENGINE_TOOLS.items()
    ]


__all__ = [
    "ALLOWED_TOOLS",
    "ENGINE_TOOLS",
    "FindSymbolArgs",
    "Normalized",
    "ReferencingSymbolsArgs",
    "SymbolsOverviewArgs",
    "code_locator",
    "entry_symbol",
    "normalize",
    "parse_args",
    "path_for",
    "record_id_for",
    "tool_definitions",
    "upstream_arguments",
    "upstream_tool",
]
