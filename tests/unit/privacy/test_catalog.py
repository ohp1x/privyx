"""Context words: a match counts only with one of them nearby."""

from __future__ import annotations

import pytest

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.privacy.detector.yaml import build_detector


def _found(text: str, **config: object) -> list[tuple[str, str]]:
    detector = build_detector({"type": "regex", "cache": False, **config})
    detection = detector.detect_sync(text, Context())  # type: ignore[attr-defined]
    return [(s.entity_type, s.text) for s in detection.merged(text).spans]


def test_a_context_word_must_be_near_a_match() -> None:
    config = {"type": "yaml", "patterns": {"PASSPORT": r"\b[A-Z]\d{7}\b"}}
    context = {"context": {"PASSPORT": ["passport", "paspor"]}}
    text = "order A1234567 shipped to the old office last week; passport no. B7654321"
    assert _found(text, **config) == [("PASSPORT", "A1234567"), ("PASSPORT", "B7654321")]
    assert _found(text, **config, **context) == [("PASSPORT", "B7654321")]
    far = "passport one two three four five six A1234567"
    assert _found(far, **config, **context) == []


def test_context_for_an_entity_the_detector_lacks_is_rejected() -> None:
    with pytest.raises(ConfigError, match="PASPORT"):
        build_detector({"type": "regex", "context": {"PASPORT": ["passport"]}})
