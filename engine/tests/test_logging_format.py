"""KE_LOG_FORMAT (text/json) and KE_LOG_FILE for the CLI and uvicorn handlers."""

from __future__ import annotations

import json
import logging
import logging.config
from pathlib import Path

import pytest

from knowledge_engine.core import logging_config
from knowledge_engine.core.logging_config import (
    JsonFormatter,
    configure_cli_logging,
    resolve_log_file,
    resolve_log_format,
    uvicorn_log_config,
)
from knowledge_engine.core.settings import Settings


def _record(msg: str = "hello %s", *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("knowledge_engine.test", logging.INFO, __file__, 7, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_object_per_line() -> None:
    line = JsonFormatter().format(
        _record("hello %s", "world", scope="alpha", color_message="\x1b[36mhello\x1b[0m")
    )
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "knowledge_engine.test"
    assert payload["line"] == 7
    assert payload["scope"] == "alpha"
    assert "color_message" not in payload
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_includes_exceptions_and_survives_bad_values() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]

    class Opaque:
        def __str__(self) -> str:
            return "opaque!"

    assert json.loads(JsonFormatter().format(_record("x", obj=Opaque())))["obj"] == "opaque!"
    # A record whose message cannot be rendered is reported, not raised.
    broken = json.loads(JsonFormatter().format(_record("%d", "not a number")))
    assert broken["level"] == "ERROR"
    assert "unformattable" in broken["message"]


def test_settings_and_resolvers_accept_and_reject_formats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert resolve_log_format({}) == "text"
    assert resolve_log_format({"KE_LOG_FORMAT": " JSON "}) == "json"
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.logging_config"):
        assert resolve_log_format({"KE_LOG_FORMAT": "xml"}) == "text"
    assert any("invalid KE_LOG_FORMAT" in r.getMessage() for r in caplog.records)
    assert resolve_log_file({"KE_LOG_FILE": " /tmp/x.log "}) == "/tmp/x.log"

    monkeypatch.setenv("KE_LOG_FORMAT", "Json")
    monkeypatch.setenv("KE_LOG_FILE", " ")
    settings = Settings()
    assert settings.log_format == "json"
    assert settings.log_file == ""
    monkeypatch.setenv("KE_LOG_FORMAT", "yaml")
    assert Settings().log_format == "text"


def test_cli_logging_json_to_stderr_and_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "logs" / "engine.jsonl"
    restore = configure_cli_logging("INFO", log_format="json", log_file=str(log_file))
    try:
        logging.getLogger("knowledge_engine.test").info("indexed %d files", 3, extra={"n": 3})
    finally:
        restore()
    err_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert json.loads(err_line)["message"] == "indexed 3 files"
    file_payload = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert file_payload["message"] == "indexed 3 files"
    assert file_payload["n"] == 3
    # restore() detached and closed both handlers.
    root = logging.getLogger()
    assert not any(isinstance(h, logging_config._CliFileHandler) for h in root.handlers)


def test_cli_logging_replaces_its_file_handler(tmp_path: Path) -> None:
    first = configure_cli_logging("INFO", log_file=str(tmp_path / "a.log"))
    second = configure_cli_logging("INFO", log_file=str(tmp_path / "b.log"))
    try:
        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, logging_config._CliFileHandler)]
        assert len(file_handlers) == 1
        logging.getLogger("knowledge_engine.test").warning("text line")
    finally:
        second()
        first()
    assert "WARNING knowledge_engine.test: text line" in (tmp_path / "b.log").read_text("utf-8")


def test_unwritable_log_file_is_reported_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    bad = str(blocker / "engine.log")
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.core.logging_config"):
        restore = configure_cli_logging("INFO", log_file=bad)
        restore()
        cfg = uvicorn_log_config("INFO", log_format="text", log_file=bad)
    assert "log_file" not in cfg["handlers"]
    assert sum("cannot open KE_LOG_FILE" in r.getMessage() for r in caplog.records) == 2


def test_uvicorn_log_config_json_and_file(tmp_path: Path) -> None:
    log_file = tmp_path / "server.jsonl"
    cfg = uvicorn_log_config("INFO", log_format="json", log_file=str(log_file))
    for formatter in cfg["formatters"].values():
        assert formatter == {"()": "knowledge_engine.core.logging_config.JsonFormatter"}
    assert cfg["handlers"]["log_file"]["filename"] == str(log_file)
    assert "log_file" in cfg["root"]["handlers"]
    assert "log_file" in cfg["loggers"]["uvicorn"]["handlers"]
    assert "log_file" in cfg["loggers"]["uvicorn.access"]["handlers"]

    # The dict is accepted by logging.config and produces JSON lines in the file.
    # dictConfig mutates process-wide loggers: snapshot every one it touches
    # and restore it, closing only the handlers it created.
    names = ["", *cfg["loggers"]]
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in names
    }
    try:
        logging.config.dictConfig(cfg)
        logging.getLogger("uvicorn.error").info("Started server process")
        logging.getLogger("knowledge_engine.server").info("CORS: allow_origins=[]")
    finally:
        for name, (handlers, level, propagate, disabled) in saved.items():
            target = logging.getLogger(name)
            for handler in list(target.handlers):
                target.removeHandler(handler)
                if handler not in handlers:
                    handler.close()
            for handler in handlers:
                target.addHandler(handler)
            target.setLevel(level)
            target.propagate = propagate
            target.disabled = disabled
    lines = [json.loads(x) for x in log_file.read_text("utf-8").splitlines()]
    assert {x["logger"] for x in lines} >= {"uvicorn.error", "knowledge_engine.server"}


def test_uvicorn_log_config_text_default_is_unchanged() -> None:
    cfg = uvicorn_log_config("INFO", log_format="text", log_file="")
    assert cfg["formatters"]["knowledge_engine"]["()"] == "uvicorn.logging.DefaultFormatter"
    assert "log_file" not in cfg["handlers"]
