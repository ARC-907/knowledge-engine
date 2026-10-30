"""``knowledge-engine module ...``: register, configure, enable/disable, inspect and invoke modules.

Every command prints JSON on stdout and goes through the same
:class:`~knowledge_engine.modules.dispatcher.ModuleHost` as the ``/modules``
HTTP routes and the MCP module tools. A typed failure is printed on stderr as
``error[<code>]: <message>`` and exits with the code's status
(:data:`knowledge_engine.knowledge.errors.CLI_EXIT_CODES`).

``module invoke`` of a long-running tool waits for the job (this process owns
it); ``Ctrl-C`` cancels it and stops the module's process group.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import CommandError

_logger = logging.getLogger(__name__)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


@contextmanager
def _host() -> Iterator[Any]:
    from ..core.settings import Settings
    from ..knowledge.errors import CLI_EXIT_CODES, KnowledgeError
    from ..modules.config import ModulesConfig
    from ..modules.dispatcher import ModuleHost
    from ..modules.evidence_ingestion import ingestion_from_settings

    host = None
    try:
        settings = Settings()
        host = ModuleHost.open(
            ModulesConfig.from_settings(settings), ingestion=ingestion_from_settings(settings)
        )
        yield host
    except KnowledgeError as exc:
        _logger.debug("module command failed", exc_info=True)
        detail = ""
        errors = exc.details.get("errors")
        if isinstance(errors, list) and errors:
            detail = " (" + "; ".join(str(e) for e in errors[:5]) + ")"
        raise CommandError(
            f"error[{exc.code}]: {exc.message}{detail}", CLI_EXIT_CODES.get(exc.code, 1)
        ) from exc
    finally:
        if host is not None:
            host.close(drain_s=2.0)


def _pairs(values: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise CommandError(f"{flag} expects NAME=VALUE, got {item!r}", 2)
        out[name.strip()] = value
    return out


def _json_arg(text: str | None, path: str | None) -> dict[str, Any]:
    if text and path:
        raise CommandError("use --input or --input-file, not both", 2)
    raw = text
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"cannot read {path}: {exc.strerror}", 2) from exc
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise CommandError(f"--input is not valid JSON: {exc}", 2) from exc
    if not isinstance(value, dict):
        raise CommandError("--input must be a JSON object", 2)
    return value


def cmd_module(args: argparse.Namespace, _parser: argparse.ArgumentParser) -> int:
    """Every ``module <verb>`` command."""
    verb = args.module_cmd
    with _host() as host:
        if verb == "discover":
            _emit({"directories": host.discover()})
        elif verb == "register":
            _emit(host.register(args.path))
        elif verb == "configure":
            _emit(
                host.configure(
                    args.module_id, _pairs(args.set, "--set"), _pairs(args.secret, "--secret")
                )
            )
        elif verb == "enable":
            _emit(host.enable(args.module_id, args.grant))
        elif verb == "disable":
            _emit(host.disable(args.module_id, drain_s=args.drain))
        elif verb == "list":
            _emit({"modules": host.list_modules()})
        elif verb == "status":
            if args.module_id:
                _emit(host.status(args.module_id, probe=args.probe))
            else:
                _emit({"modules": host.list_modules()})
        elif verb == "invoke":
            scope = None
            if args.workspace or args.project:
                scope = {
                    "workspace_id": args.workspace or host.config.default_workspace,
                    "project_id": args.project,
                }
            _emit(
                host.invoke(
                    args.module_id,
                    args.tool,
                    _json_arg(args.input, args.input_file),
                    scope=scope,
                    surface="cli",
                    principal="cli",
                    timeout_ms=args.timeout_ms,
                    effects=args.effect,
                    ingest=args.ingest,
                    wait=True,
                )
            )
        elif verb == "job":
            if args.job_cmd == "list":
                _emit({"jobs": host.job_list(args.workspace)})
            elif args.job_cmd == "status":
                _emit(host.job_status(args.job_id, args.workspace))
            else:
                _emit(host.job_cancel(args.job_id, args.workspace))
    return 0


__all__ = ["cmd_module"]
