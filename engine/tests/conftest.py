"""Suite-wide test isolation.

Every test starts from the same clean process state, whatever ran before it
under random ordering:

* **Environment** -- the environment is snapshotted, every ``KE_*`` variable is
  removed, and the exact environment is restored afterwards (including
  variables a test set directly on ``os.environ``). Without this a variable set
  by one test -- ``KE_CONFIG_PATH``, ``KE_PIPELINE_DB`` ... -- leaks into a later
  test that expects clean defaults.
* **Engine home** -- ``KE_HOME`` points at a per-test directory, so a test that
  does not configure paths can never write state into the repository checkout.
  Tests of default path discovery delete it explicitly.
* **Board sweeper** -- ``KE_BOARD_SWEEPER=0`` keeps the app lifespan from
  starting the background sweeper, which would otherwise post digests and
  reminders into the database a test is asserting on. A test that needs the
  sweeper sets ``KE_BOARD_SWEEPER=1`` itself.
* **Provider probe** -- ``KE_PROVIDER_STARTUP_PROBE=0`` keeps the app lifespan
  from probing generation providers (an HTTP call to a local Ollama) on a
  background thread; tests stay offline and deterministic. A test that needs
  the probe sets it itself.
* **Module state** -- the default sweeper is stopped and cleared, the
  process-default module host is closed, and this
  thread's cached pipeline-DB connections are closed, before and after each
  test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def _reset_engine_state() -> None:
    from knowledge_engine.agent_board import sweeper
    from knowledge_engine.modules import dispatcher as modules_dispatcher
    from knowledge_engine.runtime.foundation import db

    sweeper.reset_for_tests()
    modules_dispatcher.close_default_host()
    db.close_all()
    # One-shot FTS backfill guard is per DB path; tmp paths are never reused,
    # but clearing it keeps each test's first connection on the full init path.
    db._FTS_BACKFILLED.clear()


@pytest.fixture(autouse=True)
def _isolate_ke_env(tmp_path: Path) -> Iterator[None]:
    saved = dict(os.environ)
    for key in [k for k in os.environ if k.startswith("KE_")]:
        del os.environ[key]
    os.environ["KE_HOME"] = str(tmp_path / "ke-home")
    os.environ["KE_BOARD_SWEEPER"] = "0"
    os.environ["KE_PROVIDER_STARTUP_PROBE"] = "0"
    _reset_engine_state()
    try:
        yield
    finally:
        # Reset while the test's env still points at its databases, so a
        # sweeper lease release lands in the right file.
        _reset_engine_state()
        os.environ.clear()
        os.environ.update(saved)
