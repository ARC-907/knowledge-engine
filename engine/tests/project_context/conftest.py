"""Test isolation for the project-context suite.

Several tests (and the base engine's ``test_smoke``) set ``KE_*`` environment
variables. Without isolation a variable set by one test — most importantly
``KE_CONFIG_PATH`` — can leak into a later test that expects clean,
default/disabled configuration, producing order-dependent failures.

That isolation now lives in the suite-wide ``engine/tests/conftest.py``
(``_isolate_ke_env``), which snapshots the environment, removes every ``KE_*``
variable, and restores the exact environment afterward for every test,
including this package's. Do not redefine a fixture with the same name here:
it would shadow the root fixture for this directory.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _close_sqlite_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every SQLite connection a test opened when it ends.

    Many tests here open project/registry DBs and let them go out of scope;
    closing them at teardown keeps the run free of
    ``ResourceWarning: unclosed database`` noise without touching each test.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    yield
    for conn in opened:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
