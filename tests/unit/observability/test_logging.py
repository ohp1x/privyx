"""Tests for application logging configuration."""

from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from privyx.config.schema import Settings
from privyx.core.errors import ConfigError
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


def test_an_unwritable_log_file_is_a_config_error(_root_logging: None, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot write log_file"):
        configure_logging(Settings(log_file=str(tmp_path / "missing" / "privyx.log")))


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666, 0o700])
def test_log_file_rejects_broad_permissions(_root_logging: None, tmp_path: Path, mode: int) -> None:
    path = tmp_path / "debug.log"
    path.write_text("existing\n")
    path.chmod(mode)

    with pytest.raises(ConfigError, match="chmod 600"):
        configure_logging(Settings(log_file=str(path)))

    assert path.read_text() == "existing\n"
    assert path.stat().st_mode & 0o777 == mode


@pytest.mark.skipif(os.name != "posix", reason="POSIX file ownership")
def test_log_file_rejects_another_owner(
    _root_logging: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "debug.log"
    path.write_text("existing\n")
    path.chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)

    with pytest.raises(ConfigError, match="owned by the current user"):
        configure_logging(Settings(log_file=str(path)))

    assert path.read_text() == "existing\n"


def test_log_file_appends_to_private_file(_root_logging: None, tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    path.write_text("existing\n")
    path.chmod(0o600)
    configure_logging(Settings(log_file=str(path)), console=False)
    logging.getLogger("privyx").warning("new record")
    assert path.read_text().startswith("existing\n")
    assert "new record" in path.read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX open file replacement")
def test_log_file_writes_to_the_checked_descriptor(
    _root_logging: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "debug.log"
    original = tmp_path / "original.log"
    open_file = os.open

    def replace_after_open(filename: str, flags: int, mode: int) -> int:
        fd = open_file(filename, flags, mode)
        path.rename(original)
        path.write_text("replacement\n")
        path.chmod(0o644)
        return fd

    monkeypatch.setattr(os, "open", replace_after_open)
    configure_logging(Settings(log_file=str(path)), console=False)
    logging.getLogger("privyx").warning("private record")

    assert path.read_text() == "replacement\n"
    assert "private record" in original.read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX device file")
def test_log_file_rejects_device(_root_logging: None) -> None:
    with pytest.raises(ConfigError, match="regular file"):
        configure_logging(Settings(log_file=os.devnull))


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
