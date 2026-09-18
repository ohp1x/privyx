"""Tests for application logging configuration."""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from privyx.config.schema import Settings
from privyx.observability.logging import JsonFormatter, configure_logging


@pytest.fixture
def _root_logging() -> Iterator[None]:
    """Snapshot and restore the root logger so tests don't leak handlers."""
    root = logging.getLogger()
    privyx = logging.getLogger("privyx")
    handlers = root.handlers[:]
    level, privyx_level = root.level, privyx.level
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in handlers:
                handler.close()  # releases a log_file handle
        root.handlers[:] = handlers
        root.setLevel(level)
        privyx.setLevel(privyx_level)


def test_json_formatter_survives_quotes_and_newlines() -> None:
    """The old placeholder embedded %(message)s in a JSON template; this must not."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="privyx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='he said "hi"\nsecond line',
        args=(),
        exc_info=None,
    )

    record_json = formatter.format(record)
    parsed = json.loads(record_json)  # must not raise
    assert parsed["message"] == 'he said "hi"\nsecond line'
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "privyx"


def test_configure_logging_is_idempotent(_root_logging: None) -> None:
    configure_logging(Settings(logging="json"))
    configure_logging(Settings(logging="text"))

    root = logging.getLogger()
    privyx_handlers = [h for h in root.handlers if h.get_name() == "privyx"]
    assert len(privyx_handlers) == 1  # replaced, not stacked


def test_debug_level_does_not_leak_to_third_party(_root_logging: None) -> None:
    configure_logging(Settings(log_level="debug"))

    assert logging.getLogger("privyx.core").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("aiosqlite").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_log_file_gets_third_party_debug_console_does_not(
    _root_logging: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "debug.log"
    configure_logging(Settings(log_level="debug", log_file=str(log_file)))

    logging.getLogger("aiosqlite").debug("vault row")
    logging.getLogger("privyx.core").debug("own detail")
    for handler in logging.getLogger().handlers:
        handler.flush()

    console = capsys.readouterr().out
    assert "own detail" in console
    assert "vault row" not in console
    assert "vault row" in log_file.read_text()
    assert log_file.stat().st_mode & 0o777 == 0o600


def test_log_file_trims_third_party_chatter(_root_logging: None, tmp_path: Path) -> None:
    log_file = tmp_path / "debug.log"
    configure_logging(Settings(log_level="debug", log_file=str(log_file)))
    db, http = logging.getLogger("aiosqlite"), logging.getLogger("httpcore.http11")

    # Same shapes as aiosqlite's and httpcore's own debug calls.
    execute = functools.partial(
        sqlite3.Connection.execute, "SELECT\n  x FROM t WHERE id = ?", ("a",)
    )
    db.debug("executing %s", execute)
    db.debug("operation %s completed", execute)
    db.debug("executing %s", functools.partial(sqlite3.Connection.commit))
    http.debug("send_request_headers.started request=<Request [b'POST']>")
    http.debug("receive_response_headers.complete return_value=(b'HTTP/1.1', 200)")
    http.debug("receive_response_body.failed exception=ReadTimeout()")
    for handler in logging.getLogger().handlers:
        handler.flush()

    messages = [line.split(": ", 1)[1] for line in log_file.read_text().splitlines()]
    assert messages == [
        "SELECT x FROM t WHERE id = ? ('a',)",
        "receive_response_headers.complete return_value=(b'HTTP/1.1', 200)",
        "receive_response_body.failed exception=ReadTimeout()",
    ]
