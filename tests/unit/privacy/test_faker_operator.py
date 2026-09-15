"""Tests for :class:`FakerOperator`.

Faker is an optional extra, so this module is skipped entirely when it is not
installed rather than failing to import — the other operator tests do not depend
on it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("faker")

from privyx.core.builder import build_operator  # noqa: E402
from privyx.core.context import Context  # noqa: E402
from privyx.core.result import Detection, Span  # noqa: E402
from privyx.core.session import Session  # noqa: E402
from privyx.privacy.operator.faker import FakerOperator  # noqa: E402

TEXT = "Email alice@example.com or bob@example.com, ssn 123-45-6789"


def _detection() -> Detection:
    return Detection(
        spans=[
            Span(TEXT.index("alice@example.com"), TEXT.index("alice@example.com") + 17, "EMAIL",
                 "alice@example.com"),
            Span(TEXT.index("bob@example.com"), TEXT.index("bob@example.com") + 15, "EMAIL",
                 "bob@example.com"),
            Span(TEXT.index("123-45-6789"), TEXT.index("123-45-6789") + 11, "SSN", "123-45-6789"),
        ]
    )


# --- substitution & round-trip ---------------------------------------------


async def test_replaces_originals_with_realistic_fakes() -> None:
    result = await FakerOperator(seed=7).pseudonymize(TEXT, _detection(), Session(), Context())
    # No original value survives in the outgoing text.
    assert "alice@example.com" not in result.text
    assert "bob@example.com" not in result.text
    assert "123-45-6789" not in result.text
    # The fakes keep the *shape* of the data: emails still look like emails.
    fakes = [t.replacement for t in result.transformations]
    assert sum("@" in f for f in fakes) == 2


async def test_round_trips_through_the_session() -> None:
    operator = FakerOperator(seed=7)
    session = Session()
    result = await operator.pseudonymize(TEXT, _detection(), session, Context())
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == TEXT


async def test_same_value_reuses_its_fake() -> None:
    text = "a@b.com and a@b.com"
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com"), Span(12, 19, "EMAIL", "a@b.com")])
    result = await FakerOperator(seed=1).pseudonymize(text, detection, Session(), Context())
    fakes = [w for w in result.text.split() if "@" in w]
    assert len(fakes) == 2
    assert fakes[0] == fakes[1]


async def test_distinct_values_get_distinct_fakes_and_all_round_trip() -> None:
    emails = [f"user{i}@example.com" for i in range(20)]
    text = " ".join(emails)
    pos, spans = 0, []
    for email in emails:
        start = text.index(email, pos)
        spans.append(Span(start, start + len(email), "EMAIL", email))
        pos = start + len(email)
    operator = FakerOperator(seed=3)
    session = Session()
    result = await operator.pseudonymize(text, Detection(spans=spans), session, Context())
    # Every fake is unique, so restoration is unambiguous.
    assert len(session.mapping) == len(emails)
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == text


# --- determinism ------------------------------------------------------------


async def test_same_seed_fakes_a_value_identically() -> None:
    one = FakerOperator(seed=1)._fake_for("EMAIL", "alice@example.com")
    two = FakerOperator(seed=1)._fake_for("EMAIL", "alice@example.com")
    assert one == two


async def test_different_seed_changes_the_fake() -> None:
    one = FakerOperator(seed=1)._fake_for("EMAIL", "alice@example.com")
    two = FakerOperator(seed=2)._fake_for("EMAIL", "alice@example.com")
    assert one != two  # email space is large; a collision here would be a bug


# --- restore semantics ------------------------------------------------------


async def test_deanonymize_matches_longest_value_first() -> None:
    """A fake that is a prefix of another must not short-circuit the longer one."""
    session = Session()
    session.put("Sammy", "REAL_LONG")
    session.put("Sam", "REAL_SHORT")
    restored = await FakerOperator(seed=1).deanonymize("Sammy and Sam", session, Context())
    assert restored.text == "REAL_LONG and REAL_SHORT"


async def test_deanonymize_leaves_unknown_text_alone() -> None:
    operator = FakerOperator(seed=1)
    result = await operator.deanonymize("nothing to restore here", Session(), Context())
    assert result.text == "nothing to restore here"
    assert result.transformations == []


async def test_unknown_entity_type_still_round_trips() -> None:
    text = "project PHOENIX is internal"
    detection = Detection(spans=[Span(8, 15, "CODENAME", "PHOENIX")])
    operator = FakerOperator(seed=5)
    session = Session()
    result = await operator.pseudonymize(text, detection, session, Context())
    assert "PHOENIX" not in result.text
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == text


# --- builder wiring ---------------------------------------------------------


def test_operator_declares_literal_stream_restore() -> None:
    """The proxies rely on this attribute to pick the trie streaming recognizer."""
    assert FakerOperator(seed=1).stream_restore == "literal"


async def test_builder_builds_faker_with_locale_and_seed() -> None:
    operator = build_operator({"type": "faker", "locale": "en_US", "seed": 42})
    assert isinstance(operator, FakerOperator)
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    result = await operator.pseudonymize("a@b.com", detection, Session(), Context())
    assert result.text != "a@b.com"
