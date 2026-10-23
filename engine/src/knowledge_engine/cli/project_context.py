"""``knowledge-engine project-context ...``: the project-context CLI."""

from __future__ import annotations

import argparse


def cmd_project_context(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    from ..project_context.cli import main as pd_main

    return pd_main(args.pd_args)
