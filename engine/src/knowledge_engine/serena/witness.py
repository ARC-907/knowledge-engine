"""The compatibility witness: what the adapter was verified against (``witness.json``).

Recorded from a real Serena 2.1.0 over stdio MCP (protocol ``2026-07-28``):
the server name and version, the full upstream tool list, and the SHA-256 of
the canonical JSON of each **mapped** tool's input schema. On every session
start the adapter compares the live server with it:

* ``serverInfo.name`` must be ``Serena`` and ``serverInfo.version`` must equal
  ``KE_SERENA_VERSION`` (default: the witness version);
* every mapped tool must be listed, and its input schema must hash to the
  recorded value -- a renamed argument or a changed default would silently
  change what a call means, so any drift is ``incompatible``.

Tools that are not mapped are never forwarded, so their schemas are not
checked; new upstream tools are listed in the session's diagnostics only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from ..knowledge.contracts import canonical_json


@dataclass(frozen=True)
class Witness:
    """The recorded compatibility facts of one Serena release."""

    server_name: str
    server_version: str
    protocol: str
    tool_schema_sha256: dict[str, str]
    upstream_tools: tuple[str, ...]


@cache
def load_witness() -> Witness:
    """The witness shipped with the package."""
    raw = resources.files(__package__).joinpath("witness.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    return Witness(
        server_name=str(data["server_name"]),
        server_version=str(data["server_version"]),
        protocol=str(data["protocol"]),
        tool_schema_sha256={str(k): str(v) for k, v in data["tool_schema_sha256"].items()},
        upstream_tools=tuple(str(t) for t in data["upstream_tools"]),
    )


def schema_hash(schema: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of a tool input schema."""
    return "sha256:" + hashlib.sha256(canonical_json(dict(schema)).encode("utf-8")).hexdigest()


def compatibility_problems(
    server_name: str | None,
    server_version: str | None,
    tool_schemas: Mapping[str, Mapping[str, Any]],
    *,
    expected_version: str,
    witness: Witness | None = None,
) -> list[str]:
    """Every reason the live server differs from the witness (empty: compatible)."""
    record = witness or load_witness()
    problems: list[str] = []
    if server_name != record.server_name:
        problems.append(f"server name is {server_name!r}, expected {record.server_name!r}")
    if server_version != expected_version:
        problems.append(f"server version is {server_version!r}, expected {expected_version!r}")
    for tool, expected_hash in sorted(record.tool_schema_sha256.items()):
        schema = tool_schemas.get(tool)
        if schema is None:
            problems.append(f"tool {tool!r} is not offered")
        elif schema_hash(schema) != expected_hash:
            problems.append(f"the input schema of {tool!r} differs from the recorded witness")
    return problems


__all__ = ["Witness", "compatibility_problems", "load_witness", "schema_hash"]
