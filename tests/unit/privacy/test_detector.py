"""Tests for detector components."""

from __future__ import annotations

import pytest

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.privacy.detector.builtin import RegexDetector, YamlDetector
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
