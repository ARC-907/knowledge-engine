"""The engine's OpenAPI document is pinned in ``tests/snapshots/openapi.json``.

Any change to a route path, parameter, request body or response model shows
up as a diff here, so API changes are always deliberate and reviewed.

To accept an intended change, regenerate the snapshot and commit it::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_openapi_snapshot.py

(run from ``engine/``), then review ``git diff tests/snapshots/openapi.json``.
The package version is blanked so releases do not churn the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from knowledge_engine.core.settings import Settings

SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi.json"


def _document() -> dict[str, Any]:
    from knowledge_engine.app import create_app

    # Default surface: board on, MCP-over-HTTP off (it is not an OpenAPI route).
    spec = create_app(Settings(board_enabled=True, mcp_http=False)).openapi()
    spec["info"]["version"] = "<version>"
    return spec


def _render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_openapi_matches_snapshot() -> None:
    rendered = _render(_document())
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(rendered, encoding="utf-8")
    assert SNAPSHOT.is_file(), (
        "missing snapshot; run with UPDATE_SNAPSHOTS=1 (see module docstring)"
    )
    expected = SNAPSHOT.read_text(encoding="utf-8")
    assert rendered == expected, (
        "OpenAPI document changed; if intended, regenerate with UPDATE_SNAPSHOTS=1 "
        "(see tests/test_openapi_snapshot.py)"
    )


def test_every_engine_route_documents_its_response() -> None:
    """Each engine operation's success response has a concrete schema (a response model)."""
    spec = _document()
    engine_prefixes = ("/health", "/info", "/registry", "/search", "/generate")
    operations = [
        (path, method, op)
        for path, item in spec["paths"].items()
        if path.startswith(engine_prefixes)
        for method, op in item.items()
    ]
    assert len(operations) >= 12
    for path, method, op in operations:
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema.get("$ref") or schema.get("type") == "array", (method, path, schema)
        if schema.get("type") == "array":
            assert schema["items"], (method, path)
