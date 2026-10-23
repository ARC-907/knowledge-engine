"""The shared parsers for environment values.

:class:`knowledge_engine.core.settings.Settings` validates its boolean fields
with :func:`parse_bool` and splits its comma-separated fields with
:func:`parse_list`; modules that still read a variable directly use
:func:`env_bool` / :func:`env_list` so every flag and list in the engine
accepts the same spellings.

Booleans: ``1``/``true``/``yes``/``on`` are on and ``0``/``false``/``no``/``off``
and the empty string are off (case and surrounding whitespace ignored).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

_logger = logging.getLogger(__name__)

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def parse_bool(value: Any) -> Any:
    """Map a recognised boolean spelling to ``True``/``False``.

    Any other string is returned unchanged (so a pydantic validator rejects
    it and the caller's lenient fallback reports it); non-strings pass through.
    """
    if not isinstance(value, str):
        return value
    text = value.strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return value


def parse_bool_or(raw: str | None, default: bool) -> bool:
    """:func:`parse_bool` for a raw value; unset or unrecognised → ``default``."""
    if raw is None:
        return default
    parsed = parse_bool(raw)
    return parsed if isinstance(parsed, bool) else default


def env_bool(name: str, default: bool = False, *, env: Mapping[str, str] | None = None) -> bool:
    """The boolean value of environment variable ``name``.

    Unset → ``default``. An unrecognised spelling is logged at WARNING and
    ``default`` is used instead of failing.
    """
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    parsed = parse_bool(raw)
    if isinstance(parsed, bool):
        return parsed
    _logger.warning("ignoring invalid %s=%r (expected a boolean); using %r", name, raw, default)
    return default


def parse_list(raw: str | None, *, sep: str = ",", lower: bool = False) -> list[str]:
    """Split ``raw`` on ``sep``, strip each item and drop the empty ones."""
    if not raw:
        return []
    items = (part.strip() for part in raw.split(sep))
    return [item.lower() if lower else item for item in items if item]


def env_list(
    name: str,
    *,
    sep: str = ",",
    lower: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Environment variable ``name`` parsed with :func:`parse_list` (unset → ``[]``)."""
    source = os.environ if env is None else env
    return parse_list(source.get(name), sep=sep, lower=lower)


__all__ = [
    "FALSE_VALUES",
    "TRUE_VALUES",
    "env_bool",
    "env_list",
    "parse_bool",
    "parse_bool_or",
    "parse_list",
]
