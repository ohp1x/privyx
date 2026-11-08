"""The startup check for custom regexes that can backtrack catastrophically."""

from __future__ import annotations

import logging

import pytest

from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, OPTIONAL_PATTERNS
from privyx.privacy.detector.yaml import backtracks, build_detector


@pytest.mark.parametrize(
    "pattern",
    [
        r"^(a+)+$",
        r"([a-zA-Z]+)*$",
        r"(\w+\s?)*$",
        r"(a+|b)+",
        r"(a*)*",
        r"((ab)*)+",
        r"(?>(.+)+Z)",  # inside an atomic group
        r"(?:(.+)+Z)++",  # inside a possessive repeat
    ],
)
def test_nested_repeats_are_flagged(pattern: str) -> None:
    assert backtracks(pattern)


@pytest.mark.parametrize(
    "pattern",
    [r"(?:[a-z]+\.)+com", r"(a++)+", r"(?>a+)+", r"\d+", r"(?:x(a+))+", r"(a+){2}"],
)
def test_safe_patterns_pass(pattern: str) -> None:
    assert not backtracks(pattern)


def test_no_builtin_pattern_is_flagged() -> None:
    builtins = {**DEFAULT_PATTERNS, **OPTIONAL_PATTERNS}
    assert [name for name, pattern in builtins.items() if backtracks(pattern)] == []


def test_a_risky_custom_pattern_warns_but_still_builds(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="privyx"):
        detector = build_detector({"type": "regex", "patterns": {"ORDER": r"ORD-(\d+)+X"}})
    assert detector is not None
    assert "'ORDER'" in caplog.text and "(a+)+" in caplog.text


def test_terms_never_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="privyx"):
        build_detector({"type": "regex", "terms": {"PROJECT": ["aaa+", "(a+)+"]}})
    assert caplog.text == ""
