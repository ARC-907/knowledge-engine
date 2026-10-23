"""Compatibility alias for :mod:`knowledge_engine.agent_board.http`.

The board router, its trust gate and its limits moved to
``knowledge_engine.agent_board.http`` (the board owns its HTTP surface).
Importing this module yields that very module object, so existing imports,
attribute access and ``monkeypatch.setattr`` on ``board_routes`` keep acting
on the live router and helpers.
"""

from __future__ import annotations

import sys

from ...agent_board import http as _board_http

sys.modules[__name__] = _board_http
