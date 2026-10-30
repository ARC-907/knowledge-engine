"""Shared browser-test plumbing: serving an app, launching Chromium, and
saving a screenshot plus console errors when a dashboard assertion fails."""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    setattr(item, f"rep_{report.when}", report)


# Read at collection time: the suite-wide fixture strips every KE_* variable
# before a test body runs.
_CHROMIUM_OVERRIDE = os.environ.get("KE_UI_CHROMIUM")
# When set (CI does), a failing test leaves a full-page screenshot and the
# browser console errors here.
_ARTIFACT_DIR = os.environ.get("KE_UI_ARTIFACT_DIR")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _serve(app: Any) -> Iterator[str]:
    """Run ``app`` with uvicorn on a free loopback port in a thread; yield its URL."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="ke-ui-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def serve() -> Any:
    """``with serve(app) as url:`` runs ``app`` on a free loopback port for the test."""
    return _serve


def _chromium_candidates() -> list[str]:
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "",
        str(Path.home() / ".cache" / "ms-playwright"),
    ]
    found: list[str] = [_CHROMIUM_OVERRIDE] if _CHROMIUM_OVERRIDE else []
    for root in filter(None, roots):
        found.extend(str(p) for p in sorted(Path(root).glob("chromium*/chrome-linux/chrome")))
    return [c for c in found if Path(c).is_file()]


def _launch_chromium(pw: Any) -> Any:
    """Prefer Playwright's own browser; fall back to any installed Chromium."""
    try:
        return pw.chromium.launch()
    except Exception as exc:  # any launch failure means "try the fallbacks"
        first_error = exc
    for candidate in _chromium_candidates():
        try:
            return pw.chromium.launch(executable_path=candidate)
        except Exception:  # noqa: S112 - try the next build
            continue
    pytest.skip(f"no usable Chromium for Playwright: {first_error}")
    raise AssertionError("unreachable: pytest.skip() raises")  # keeps every path explicit


def _save_failure_artifacts(request: pytest.FixtureRequest, page: Any) -> None:
    report = getattr(request.node, "rep_call", None)
    if not _ARTIFACT_DIR or report is None or not report.failed:
        return
    out = Path(_ARTIFACT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    name = request.node.name.replace("/", "_")
    try:
        page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    except Exception as exc:  # the page may already be broken; keep the test's own failure
        (out / f"{name}.screenshot-error.txt").write_text(str(exc), encoding="utf-8")
    errors = getattr(page, "errors", [])
    (out / f"{name}.page-errors.txt").write_text("\n".join(errors), encoding="utf-8")


@pytest.fixture
def page(request: pytest.FixtureRequest) -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api", reason="install the `ui` group")
    with sync_api.sync_playwright() as pw:
        browser = _launch_chromium(pw)
        context = browser.new_context(color_scheme="light")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.errors = errors  # type: ignore[attr-defined]
        try:
            yield page
            _save_failure_artifacts(request, page)
        finally:
            context.close()
            browser.close()
