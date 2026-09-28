"""Tests for detector components."""

from __future__ import annotations

import re
import time

import pytest

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector, YamlDetector
from privyx.privacy.detector.yaml import build_detector


@pytest.mark.asyncio
async def test_regex_detector_finds_email() -> None:
    detector = RegexDetector()
    detection = await detector.detect("Email: alice@example.com", Context())
    assert len(detection.spans) == 1
    span = detection.spans[0]
    assert span.entity_type == "EMAIL"
    assert span.text == "alice@example.com"


@pytest.mark.asyncio
async def test_regex_detector_finds_multiple_entities() -> None:
    detector = RegexDetector()
    text = "alice@example.com +1 (555) 123-4567 192.168.0.1"
    detection = await detector.detect(text, Context())
    types = {s.entity_type for s in detection.spans}
    assert "EMAIL" in types
    assert "PHONE" in types
    assert "IP_ADDRESS" in types


@pytest.mark.asyncio
async def test_yaml_detector_custom_rules() -> None:
    detector = YamlDetector({"CODE": r"\b[A-Z]{3}\d{2}\b"})
    detection = await detector.detect("code is ABC12", Context())
    assert len(detection.spans) == 1
    assert detection.spans[0].entity_type == "CODE"
    assert detection.spans[0].text == "ABC12"


@pytest.mark.asyncio
async def test_no_false_positive_on_plain_text() -> None:
    detector = RegexDetector()
    detection = await detector.detect("hello world, nothing sensitive here", Context())
    assert not detection


@pytest.mark.parametrize(
    "address",
    [
        "a@b.io",
        "first.last+tag@mail.example.co.uk",
        # At the RFC 5321 limits: 64-character local part, 251-character domain.
        "x" * 64 + "@" + "sub." * 58 + "example.photography",
    ],
)
def test_email_matches_the_whole_address(address: str) -> None:
    detection = RegexDetector().detect_sync(f"mail {address} now", Context())
    assert [s.text for s in detection.spans] == [address]


@pytest.mark.parametrize(
    ("text", "cards"),
    [
        ("4111 1111 1111 1111 or 378282246310005", ["4111 1111 1111 1111", "378282246310005"]),
        # Both fail the Luhn check: a millisecond timestamp and an order ID.
        ("ts 1790517229401 order 1234567812345678", []),
    ],
)
def test_credit_card_must_pass_the_luhn_check(text: str, cards: list[str]) -> None:
    detection = RegexDetector().detect_sync(text, Context())
    assert [s.text for s in detection.spans if s.entity_type == "CREDIT_CARD"] == cards


def test_own_pattern_for_a_builtin_entity_is_used_as_written() -> None:
    detector = YamlDetector({"CREDIT_CARD": r"\b\d{16}\b"})
    detection = detector.detect_sync("id 1234567812345678", Context())
    assert [s.text for s in detection.spans] == ["1234567812345678"]


def _fill(unit: str, size: int = 100_000) -> str:
    return (unit * (size // len(unit) + 1))[:size]


#: Near-misses that make an unbounded quantifier backtrack: long runs of one
#: character class, and the prefixes the built-in patterns look for.
_ADVERSARIAL = {
    "letters": _fill("a"),
    "hex": _fill("0123456789abcdef"),
    "dotted": _fill("abc."),
    "digits": _fill("1 "),
    "spaced digits": "1" + " " * 100_000 + "1",
    "dashes": _fill("-"),
    "at signs": _fill("a@"),
    "long domain": "a@" + _fill("b"),
    "dotted domain": "a@" + _fill("b."),
    "local parts": _fill("b" * 64 + "@" + "c" * 255),
    "key header": "-----BEGIN RSA PRIVATE KEY-----\n" + _fill("A"),
    "jwt": "eyJ" + _fill("a") + ".eyJ",
    "schemes": _fill("a://"),
    "bearer": _fill("bearer "),
    "whitespace": _fill("a b\n"),
    "escaped newlines": _fill("\\n"),
    "open quote": 'password="' + _fill("x"),
    "assignments": _fill("token="),
}


def test_builtin_patterns_stay_linear_on_adversarial_input() -> None:
    # Each takes ~20 ms at most here; the unbounded EMAIL pattern took ~8 s on
    # several of these inputs and froze the event loop for that long.
    slow = []
    for entity, pattern in DEFAULT_PATTERNS.items():
        compiled = re.compile(pattern)
        for shape, text in _ADVERSARIAL.items():
            start = time.perf_counter()
            for _ in compiled.finditer(text):
                pass
            elapsed = time.perf_counter() - start
            if elapsed > 0.5:
                slow.append(f"{entity} on {shape}: {elapsed:.1f} s")
    assert not slow


@pytest.mark.asyncio
async def test_value_group_marks_only_the_value() -> None:
    # `value` group → only it; an alternative without it → the whole match;
    # an empty `value` → nothing.
    detector = YamlDetector({"SECRET": r"PASSWORD=(?P<value>\S*)|sk-\w+"})
    detection = await detector.detect("DB_PASSWORD=hunter2 sk-abc PASSWORD= x", Context())
    assert [(s.entity_type, s.text) for s in detection.spans] == [
        ("SECRET", "hunter2"),
        ("SECRET", "sk-abc"),
    ]
    assert detection.spans[0].start == len("DB_PASSWORD=")


async def _detect_terms(text: str, **config: object) -> dict[str, list[str]]:
    """Build a detector from a `terms` config and return {entity: [matched text]}."""
    detector = build_detector({"type": "regex", **config})
    detection = (await detector.detect(text, Context())).merged(text)
    found: dict[str, list[str]] = {}
    for span in detection.spans:
        found.setdefault(span.entity_type, []).append(span.text)
    return found


@pytest.mark.asyncio
async def test_terms_longest_match_wins() -> None:
    found = await _detect_terms("hi annbob", terms={"PERSON": ["ann", "bob", "annbob"]})
    assert found == {"PERSON": ["annbob"]}


@pytest.mark.asyncio
async def test_terms_match_case_insensitively() -> None:
    found = await _detect_terms("Ann and ANN and ann", terms={"PERSON": ["ann"]})
    assert found["PERSON"] == ["Ann", "ANN", "ann"]


@pytest.mark.asyncio
async def test_terms_respect_word_boundaries() -> None:
    found = await _detect_terms("annual report by deann", terms={"PERSON": ["ann"]})
    assert found == {}


@pytest.mark.asyncio
async def test_terms_match_a_url_verbatim() -> None:
    url = "https://git.internal.example/team"
    found = await _detect_terms(f"see {url} today", terms={"URL": [url]})
    assert found == {"URL": [url]}


@pytest.mark.asyncio
async def test_terms_keep_the_builtin_patterns() -> None:
    found = await _detect_terms("ann at a@example.com", terms={"PERSON": ["ann"]})
    assert found["PERSON"] == ["ann"]
    assert found["EMAIL"] == ["a@example.com"]


@pytest.mark.asyncio
async def test_terms_and_patterns_coexist() -> None:
    found = await _detect_terms(
        "ann owns EMP-123456",
        terms={"PERSON": ["ann"]},
        patterns={"EMPLOYEE_ID": r"\bEMP-\d{6}\b"},
    )
    assert found["PERSON"] == ["ann"]
    assert found["EMPLOYEE_ID"] == ["EMP-123456"]


@pytest.mark.asyncio
async def test_patterns_win_over_terms_for_the_same_entity() -> None:
    found = await _detect_terms(
        "ann and bob",
        terms={"PERSON": ["ann"]},
        patterns={"PERSON": r"\bbob\b"},
    )
    assert found == {"PERSON": ["bob"]}


@pytest.mark.asyncio
async def test_empty_term_list_matches_nothing() -> None:
    found = await _detect_terms("anything at all", terms={"PERSON": [], "ORG": ["  "]})
    assert found == {}


@pytest.mark.parametrize(
    "terms",
    [
        "ann",  # not a mapping
        {"PERSON": "ann"},  # a bare string, not a list
        {"PERSON": 42},
    ],
)
def test_malformed_terms_raise_config_error(terms: object) -> None:
    with pytest.raises(ConfigError):
        build_detector({"type": "regex", "terms": terms})


@pytest.mark.parametrize("entity", ["my-person", "1PERSON", "", "A" * 65])
def test_invalid_entity_name_raises_config_error(entity: str) -> None:
    with pytest.raises(ConfigError, match="invalid entity name"):
        build_detector({"type": "regex", "terms": {entity: ["ann"]}})


@pytest.mark.asyncio
async def test_detector_list_pools_spans_from_every_detector() -> None:
    detector = build_detector(
        [{"type": "regex"}, {"type": "yaml", "patterns": {"CODE": r"\b[A-Z]{3}\d{2}\b"}}]
    )
    detection = await detector.detect("mail alice@example.com code ABC12", Context())
    assert sorted((s.entity_type, s.text) for s in detection.spans) == [
        ("CODE", "ABC12"),
        ("EMAIL", "alice@example.com"),
    ]


@pytest.mark.asyncio
async def test_detector_list_collapses_exact_duplicates() -> None:
    detector = build_detector([{"type": "regex"}, {"type": "regex"}])
    detection = await detector.detect("mail alice@example.com", Context())
    assert len(detection.spans) == 1


def test_empty_detector_list_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="must not be empty"):
        build_detector([])
