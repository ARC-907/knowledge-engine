"""``knowledge-engine board ...``: the agent-board HTTP client (``agent_board.cli``)."""

from __future__ import annotations

import argparse


def cmd_board(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..agent_board.cli import main as board_main

    return board_main(args.board_args)
