"""Expose each test phase's report to fixtures, so the browser fixture can
capture a screenshot when a dashboard assertion fails."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    setattr(item, f"rep_{report.when}", report)
