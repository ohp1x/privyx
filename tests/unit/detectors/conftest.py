"""Print the detection eval's scores after the test run, where CI logs keep them."""

from __future__ import annotations

import pytest

_SUMMARY: list[str] = []


@pytest.fixture
def summary() -> list[str]:
    """Text to print in the test run's summary."""
    return _SUMMARY


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _SUMMARY:
        terminalreporter.write_sep("-", "detection eval")
        for text in _SUMMARY:
            terminalreporter.write_line(text)
