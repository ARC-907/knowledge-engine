"""Test isolation for the project-docs suite.

Several tests (and the base engine's ``test_smoke``) set ``KE_*`` environment
variables. Without isolation a variable set by one test — most importantly
``KE_CONFIG_PATH`` — can leak into a later test that expects clean,
default/disabled configuration, producing order-dependent failures. This
autouse fixture snapshots the environment, removes any ``KE_*`` variables so
config discovery starts from a known-clean state, and restores the exact
environment afterward.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_ke_env():
    saved = dict(os.environ)
    for key in [k for k in os.environ if k.startswith("KE_")]:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
