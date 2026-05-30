"""Reference widget service for the sample project.

This module exists so the docstring detector and pointer planner have a
realistic, generic target to operate on. Nothing here is real.
"""

from __future__ import annotations


def validate(request: dict) -> bool:
    """Validate an incoming widget request.

    A request is valid when it carries a non-empty ``name`` and a positive
    ``quantity``. This docstring is deliberately verbose so it reads as a
    plausible pointer-replacement candidate during scanner demos.
    """
    return bool(request.get("name")) and request.get("quantity", 0) > 0


def handle(request: dict) -> dict:
    """Handle a validated widget request and return a result envelope."""
    if not validate(request):
        return {"ok": False, "error": "invalid request"}
    return {"ok": True, "name": request["name"], "quantity": request["quantity"]}
