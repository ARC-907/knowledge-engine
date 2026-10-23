"""Knowledge-Engine -- Pipeline SQLite backbone.

Single database file provides durable state for queue, registry, message board,
tools, and chat. WAL mode for concurrent reads. The schema is created and
upgraded on first connection by numbered migrations (``schema/NNN_*.sql``).

Env vars:
    KE_PIPELINE_DB     (default: <data_dir>/pipeline.db, where data_dir is $KE_DATA_DIR
                        or $KE_HOME/.knowledge-engine/data -- see core/paths.py)

This is a coordination store, not a document store. Research objects, source
records, and handoff bundles remain on the filesystem for inspectability.

Package layout (every public name is re-exported here, so
``from knowledge_engine.runtime.foundation import db`` keeps working):

* :mod:`.connection`   -- path resolution, ``using_db`` scope routing, the
  bounded per-(thread, path) connection cache, ``close_all``.
* :mod:`.migrate`      -- schema migrations (shared runner in
  ``core.util.sqlite``), pre-versioned DB adoption, FTS backfill.
* :mod:`.board_config` -- ``board_config`` defaults + first-boot seed.
* :mod:`.leases`       -- the board sweeper lease.
* :mod:`.locks`        -- process-local locks (master key, scope creation).
* :mod:`.rows`         -- row -> dict helpers, ``require_row``.
* :mod:`.events`       -- the ``events`` audit log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .board_config import (
    BOARD_CONFIG_DEFAULT_CHANNELS,
    BOARD_CONFIG_DEFAULTS,
    default_board_config,
    seed_board_config,
)
from .connection import (
    MAX_CONNECTIONS_PER_THREAD,
    PIPELINE_DB_ENV,
    PIPELINE_DB_FILENAME,
    active_scope_db_path,
    close_all,
    close_connection,
    current_db_path,
    get_connection,
    open_connection_count,
    resolve_db_path,
    using_db,
)
from .events import log_event
from .leases import SWEEPER_LEASE_KEY, _parse_lease, acquire_sweeper_lease, release_sweeper_lease
from .locks import master_key_lock, scope_creation_lock
from .migrate import (
    _FTS_BACKFILLED,
    BASELINE_VERSION,
    SCHEMA_DIR,
    _backfill_messages_fts_once,
    init_schema,
)
from .rows import (
    JSON_FIELDS,
    Row,
    RowNotFoundError,
    dict_from_row,
    require_row,
    rows_to_dicts,
)


def __getattr__(name: str) -> Any:
    # Back-compat module attribute ``DB_PATH`` (PEP 562): resolved on access
    # rather than at import, so importing this package touches neither the
    # environment nor the filesystem. Prefer ``resolve_db_path()``.
    if name == "DB_PATH":
        return Path(resolve_db_path()).resolve()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Historical private name, kept for callers that imported it.
_JSON_FIELDS = JSON_FIELDS

__all__ = [
    "BASELINE_VERSION",
    "BOARD_CONFIG_DEFAULTS",
    "BOARD_CONFIG_DEFAULT_CHANNELS",
    "JSON_FIELDS",
    "MAX_CONNECTIONS_PER_THREAD",
    "PIPELINE_DB_ENV",
    "PIPELINE_DB_FILENAME",
    "SCHEMA_DIR",
    "SWEEPER_LEASE_KEY",
    "_FTS_BACKFILLED",
    "_JSON_FIELDS",
    "Row",
    "RowNotFoundError",
    "_backfill_messages_fts_once",
    "_parse_lease",
    "acquire_sweeper_lease",
    "active_scope_db_path",
    "close_all",
    "close_connection",
    "current_db_path",
    "default_board_config",
    "dict_from_row",
    "get_connection",
    "init_schema",
    "log_event",
    "master_key_lock",
    "open_connection_count",
    "release_sweeper_lease",
    "require_row",
    "resolve_db_path",
    "rows_to_dicts",
    "scope_creation_lock",
    "seed_board_config",
    "using_db",
]
