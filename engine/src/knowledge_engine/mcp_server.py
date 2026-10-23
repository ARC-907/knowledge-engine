"""Deprecated alias for :mod:`knowledge_engine.mcp.server`.

Launch the server with ``knowledge-engine mcp`` or
``python -m knowledge_engine.mcp.server``; build one in code with
:func:`knowledge_engine.mcp.server.build_server`. ``python -m
knowledge_engine.mcp_server`` keeps working until this alias is removed.
"""

from __future__ import annotations

import sys
import warnings

from .mcp.server import main

warnings.warn(
    "knowledge_engine.mcp_server is deprecated; run `knowledge-engine mcp` or "
    "`python -m knowledge_engine.mcp.server`, or import knowledge_engine.mcp.server",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["main"]

if __name__ == "__main__":
    # The DeprecationWarning above is attributed to runpy under ``python -m``
    # and silenced by the default filters, so say it where the operator looks.
    sys.stderr.write(
        "knowledge_engine.mcp_server is deprecated; launch the server with "
        "`knowledge-engine mcp` (or `python -m knowledge_engine.mcp.server`) instead.\n"
    )
    sys.exit(main())
