"""Tests for the log-injection guard used on request-derived log fields."""

from __future__ import annotations

import logging

import pytest
from hypothesis import given, strategies as st

from knowledge_engine.core.logsafe import log_safe


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("a\nb", "a\\nb"),
        ("a\rb", "a\\rb"),
        ("a\r\nb", "a\\r\\nb"),
        ("", ""),
        (42, "42"),
        (None, "None"),
    ],
)
def test_log_safe_escapes_line_breaks(value: object, expected: str) -> None:
    assert log_safe(value) == expected


@given(st.text())
def test_log_safe_never_yields_a_line_break(value: str) -> None:
    out = log_safe(value)
    assert "\n" not in out
    assert "\r" not in out


def test_forged_log_line_stays_on_one_record(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("knowledge_engine.test_logsafe")
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("scope %r created", log_safe("x\n2026-01-01 INFO board key created"))
    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].getMessage()
