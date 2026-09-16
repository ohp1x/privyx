"""Presidio detector — entity naming and config wiring.

Presidio itself is an optional extra, so these tests inject a stand-in analyzer.
What is actually at risk here is not Presidio's NER (that is Presidio's problem)
but the seam around it: the entity names it emits, and whether a config can
select it at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.privacy.detector.presidio import PresidioDetector
from privyx.privacy.detector.yaml import build_detector
from privyx.privacy.policy.strict import StrictPolicy


@dataclass
class _Result:
    entity_type: str
    start: int
    end: int
    score: float = 0.9


class _FakeAnalyzer:
    """Stands in for ``AnalyzerEngine``, recording how it was called."""

    def __init__(self, results: list[_Result]) -> None:
        self._results = results
        self.calls: list[dict] = []

    def analyze(self, **kwargs: object) -> list[_Result]:
        self.calls.append(dict(kwargs))
        return self._results


async def test_presidio_names_are_translated_for_the_strict_policy() -> None:
    """EMAIL_ADDRESS must reach the policy as EMAIL, or strict drops the PII."""
    text = "mail ana@example.com about the invoice"
    start = text.index("ana@example.com")
    detector = PresidioDetector(
        analyzer=_FakeAnalyzer([_Result("EMAIL_ADDRESS", start, start + len("ana@example.com"))])
    )

    detection = await detector.detect(text, Context())
    assert [s.entity_type for s in detection.spans] == ["EMAIL"]
    assert detection.spans[0].text == "ana@example.com"

    kept = await StrictPolicy().decide(detection, Context())
    assert len(kept.spans) == 1, "strict policy dropped the span — the alias did not apply"


async def test_unknown_entity_types_pass_through_unchanged() -> None:
    detector = PresidioDetector(analyzer=_FakeAnalyzer([_Result("PERSON", 0, 3)]))
    detection = await detector.detect("Ana", Context())
    assert [s.entity_type for s in detection.spans] == ["PERSON"]


async def test_entities_and_threshold_reach_the_analyzer() -> None:
    analyzer = _FakeAnalyzer([])
    detector = PresidioDetector(
        analyzer=analyzer, entities=["PERSON"], score_threshold=0.7, language="id"
    )

    await detector.detect("halo", Context())

    assert analyzer.calls[0]["entities"] == ["PERSON"]
    assert analyzer.calls[0]["score_threshold"] == 0.7
    assert analyzer.calls[0]["language"] == "id"


async def test_empty_entities_means_every_recognizer() -> None:
    analyzer = _FakeAnalyzer([])
    await PresidioDetector(analyzer=analyzer).detect("halo", Context())
    assert analyzer.calls[0]["entities"] is None


def test_config_can_select_presidio() -> None:
    """``detector.type: presidio`` must be a known type.

    Before it was registered this raised ``unknown detector type``. Without
    Presidio installed it now fails on the missing extra instead — either way,
    at startup, and never with 'unknown'.
    """
    try:
        detector = build_detector({"type": "presidio", "score_threshold": 0.5})
    except ConfigError as exc:
        assert "unknown detector type" not in str(exc)
        pytest.skip(f"presidio extra not installed: {exc}")
    else:
        assert detector.name == "presidio"
