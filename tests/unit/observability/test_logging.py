"""Tests for application logging configuration."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from privyx.config.schema import Settings
from privyx.observability.logging import JsonFormatter, configure_logging


@pytest.fixture
def _root_logging() -> Iterator[None]:
    """Snapshot and restore the root logger so tests don't leak handlers."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


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
