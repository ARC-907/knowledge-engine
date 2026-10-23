"""Registry subcommands: ``registry validate [path] [--json]``.

``validate`` checks a registry file against the registry JSON Schema without
creating or modifying anything (unlike the corpus commands, which create a
missing registry). Exit status: 0 when the file is valid, 1 when it is
missing, unreadable, not JSON or does not match the schema.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


def _default_registry_path() -> Path:
    from ..core.config import Config

    return Config.from_env().registry_path


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def cmd_registry_validate(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """``knowledge-engine registry validate [path] [--json]``."""
    from ..core.registry import RegistryValidationError, load_registry_document

    path = Path(args.path) if args.path else _default_registry_path()
    try:
        load_registry_document(path)
    except RegistryValidationError as exc:
        _logger.debug("registry %s is invalid", path, exc_info=True)
        if args.json:
            _emit({"valid": False, **exc.to_dict()})
        else:
            print(exc.render(), file=sys.stderr)
        return 1
    except OSError as exc:
        message = f"cannot read the registry: {exc.strerror or exc}"
        _logger.debug("cannot read registry %s", path, exc_info=True)
        if args.json:
            _emit({"valid": False, "path": str(path), "message": message, "errors": []})
        else:
            print(f"{path}: {message}", file=sys.stderr)
        return 1
    if args.json:
        _emit({"valid": True, "path": str(path), "message": "valid", "errors": []})
    else:
        print(f"{path}: valid")
    return 0
