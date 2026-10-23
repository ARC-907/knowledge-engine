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
