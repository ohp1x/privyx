"""Folding look-alike and invisible characters before detection (``normalize``)."""

from __future__ import annotations

import pytest

from privyx.core.context import Context
from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.detector.yaml import build_detector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.vault.memory import MemoryVault


def _found(text: str, **config: object) -> list[tuple[str, str]]:
    detector = build_detector({"type": "regex", "terms": {"PERSON": ["john"]}, **config})
    spans = detector.detect_sync(text, Context())
    return sorted((s.entity_type, s.text) for s in spans.spans)


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ("call ｊｏｈｎ now", "ｊｏｈｎ"),  # fullwidth
        ("call jo\u200bhn now", "jo\u200bhn"),  # zero-width space inside
        ("call jo\u00adhn now", "jo\u00adhn"),  # soft hyphen
        ("call \ufeffjohn\u2060 now", "john"),  # format characters around it: found as is
    ],
)
def test_a_folded_value_is_found_as_written(text: str, written: str) -> None:
    assert _found(text, normalize=True) == [("PERSON", written)]


def test_without_normalize_nothing_is_folded() -> None:
    assert _found("call ｊｏｈｎ or jo\u200bhn") == []


def test_accents_and_look_alikes_are_not_folded() -> None:
    """``jóhn`` is another name; Cyrillic ``\u043e`` is not a compatibility form."""
    assert _found("jóhn jo\u0301hn j\u043ehn", normalize=True) == []


def test_text_that_folds_to_itself_finds_nothing_twice() -> None:
    for text in ("john at ana@example.com", "josé and john, café"):
        assert _found(text, normalize=True) == _found(text)


def test_a_character_that_folds_to_several_is_marked_whole() -> None:
    """``ﬁ`` folds to ``fi``: a match on either letter takes the ligature."""
    detector = RegexDetector({"FILE": r"\bfile\b"}, normalize=True)
    (span,) = detector.detect_sync("open ﬁle now", Context()).spans
    assert (span.start, span.end, span.text) == (5, 8, "ﬁle")


def test_a_value_is_found_when_only_its_context_folds() -> None:
    """The value is ASCII, the name or context word around it is fullwidth."""
    named = RegexDetector({"PW": r"PASSWORD=(?P<value>\S+)"}, normalize=True)
    spans = named.detect_sync("ＰＡＳＳＷＯＲＤ=abc123xyz", Context()).spans
    assert [(s.entity_type, s.text) for s in spans] == [("PW", "abc123xyz")]

    builtin = RegexDetector(normalize=True).detect_sync("ｐａｓｓｗｏｒｄ: hunter2xyz", Context())
    assert [(s.entity_type, s.text) for s in builtin.spans] == [("SECRET", "hunter2xyz")]

    near = {"context": {"PERSON": ["employee"]}}
    assert _found("ｅｍｐｌｏｙｅｅ john", normalize=True, **near) == [("PERSON", "john")]


def test_what_the_text_as_written_shows_is_found_once() -> None:
    """Something else folds, so the folded text is scanned too: ``john`` and
    the email are not found a second time there."""
    detector = build_detector({"type": "regex", "terms": {"PERSON": ["john"]}, "normalize": True})
    spans = detector.detect_sync("john, ｊｏｈｎ, ana@example.com", Context()).spans
    assert sorted((s.entity_type, s.start) for s in spans) == [
        ("EMAIL", 12),
        ("PERSON", 0),
        ("PERSON", 6),
    ]


def test_built_in_patterns_see_folded_text_too() -> None:
    detector = RegexDetector(normalize=True)
    spans = detector.detect_sync("mail ana@ｅｘａｍｐｌｅ.com", Context()).spans
    assert [(s.entity_type, s.text) for s in spans] == [("EMAIL", "ana@ｅｘａｍｐｌｅ.com")]


async def test_the_text_as_written_is_masked_and_restored() -> None:
    engine = PrivacyEngine(
        detector=build_detector(
            {"type": "regex", "terms": {"PERSON": ["john"]}, "normalize": True}
        ),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    text = "ask ｊｏｈｎ or jo\u200bhn"

    masked = await engine.transform(text, session=session)

    assert masked.text == "ask <PRIVYX_PERSON_1> or <PRIVYX_PERSON_2>"
    assert (await engine.restore(masked.text, session.session_id)).text == text
