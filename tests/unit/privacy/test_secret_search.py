"""Candidate scanning must preserve every SECRET match, capture, and offset."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from privyx.core.context import Context
from privyx.privacy.detector.builtin import SECRET_PATTERN, YamlDetector, _secret_matches

from .test_detection_eval import CORPUS, _parse
from .test_detector import _ADVERSARIAL

PATTERN = re.compile(SECRET_PATTERN)


def _captures(
    matches: Iterable[re.Match[str]],
) -> list[tuple[tuple[int, int], dict[str, str | None]]]:
    return [(m.span(), m.groupdict()) for m in matches]


def _assert_equivalent(text: str) -> None:
    assert _captures(_secret_matches(PATTERN, text)) == _captures(PATTERN.finditer(text))


@pytest.mark.parametrize("path", sorted(CORPUS.glob("*.txt")), ids=lambda p: p.name)
def test_secret_search_matches_reference_on_corpus(path: Path) -> None:
    text, _ = _parse(path.read_text(encoding="utf-8"))
    _assert_equivalent(text)


@pytest.mark.parametrize("text", _ADVERSARIAL.values(), ids=_ADVERSARIAL.keys())
def test_secret_search_matches_reference_on_adversarial_input(text: str) -> None:
    _assert_equivalent(text)


FRAGMENTS = [
    'password: str = "typed-canary"',
    "apiKey: string = 'typed-canary'",
    'SECRET_KEY = """first\nsecond"""',
    'private_key = r"raw-canary"',
    'access_key = b"bytes-canary"',
    "passphrase: 'canary'",
    "pwd=canary",
    "pass=canary",
    "token=canary",
    "_KEY=canary",
    "credential=canary",
    "credentials=canary",
    "curl --db-password cli-canary",
    "--token --help",
    "--bad--password canary",
    "passpassword=canary",
    "paſſword=canary",  # Unicode case folding inside a name
    "ſecret=canary",  # Unicode case folding at the candidate start
    "prıvate_key=canary",
    "APİ_KEY=canary",
    'api_key=os.environ["API_KEY"]',
    'password="token=inside-rejected..." token=after',
    "password=token=following",
    "password=",
    'password="',
    'password="""',
]


@pytest.mark.parametrize("text", FRAGMENTS)
def test_secret_search_preserves_flags_unicode_and_consumed_matches(text: str) -> None:
    _assert_equivalent(text)
    _assert_equivalent(text.upper())
    _assert_equivalent(text.swapcase())


@given(
    st.lists(st.one_of(st.text(max_size=80), st.sampled_from(FRAGMENTS)), max_size=30).map("".join)
)
def test_secret_search_matches_reference_with_arbitrary_surroundings(text: str) -> None:
    _assert_equivalent(text)


def test_custom_secret_pattern_keeps_its_own_match_starts() -> None:
    detector = YamlDetector({"SECRET": r"CUSTOM\[(?P<value>[^]]+)\]"})
    detection = detector.detect_sync("CUSTOM[canary]", Context())
    assert [(s.start, s.end, s.text) for s in detection.spans] == [(7, 13, "canary")]
