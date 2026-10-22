"""Knowledge Engine — corpus-agnostic registry, indexer, search, MCP."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("knowledge-engine")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
