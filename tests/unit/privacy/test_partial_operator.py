"""Tests for :class:`PartialOperator`."""

from __future__ import annotations

import pytest

from privyx.core.builder import build_operator
from privyx.core.context import Context
from privyx.core.result import Detection, Span
from privyx.core.session import Session
from privyx.privacy.operator.partial import PartialOperator, mask


def _detect(text: str, *values: tuple[str, str]) -> Detection:
    return Detection(
        spans=[Span(text.index(v), text.index(v) + len(v), kind, v) for kind, v in values]
    )


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("alice@example.com", "a****@example.com"),
        ("4111 1111 1111 1234", "**** **** **** 1234"),
        ("+62 812-3456-7890", "+** ***-****-7890"),
        ("123-45-6789", "***-**-*789"),  # a third of nine digits stays
        ("sk-proj-abcdEFGH12345678wxyz", "**-****-****************wxyz"),
    ],
)
def test_masks_all_but_a_little_context(value: str, masked: str) -> None:
    assert mask(value) == masked


def test_a_short_value_never_masks_to_markdown_emphasis() -> None:
    assert mask("Bob") == "****b"


async def test_round_trips_through_the_session() -> None:
    text = "mail alice@example.com, card 4111 1111 1111 1234"
    detection = _detect(
        text, ("EMAIL", "alice@example.com"), ("CREDIT_CARD", "4111 1111 1111 1234")
    )
    operator, session = PartialOperator(), Session()

    masked = await operator.pseudonymize(text, detection, session, Context())

    assert masked.text == "mail a****@example.com, card **** **** **** 1234"
    restored = await operator.deanonymize(f"Re: {masked.text}", session, Context())
    assert restored.text == f"Re: {text}"


async def test_values_that_mask_alike_get_distinct_masks() -> None:
    text = "4111 1111 1111 1234 and 5500 0000 0000 1234"
    detection = _detect(
        text, ("CREDIT_CARD", "4111 1111 1111 1234"), ("CREDIT_CARD", "5500 0000 0000 1234")
    )
    operator, session = PartialOperator(), Session()

    masked = await operator.pseudonymize(text, detection, session, Context())

    assert masked.text == "**** **** **** 1234 and ***** **** **** 1234"
    assert (await operator.deanonymize(masked.text, session, Context())).text == text


async def test_text_the_session_did_not_issue_is_left_alone() -> None:
    session = Session()
    session.put("****b", "Bob")
    restored = await PartialOperator().deanonymize("**bold** and ****b", session, Context())
    assert restored.text == "**bold** and Bob"


def test_built_from_config() -> None:
    operator = build_operator({"type": "partial"})
    assert isinstance(operator, PartialOperator)
    assert operator.stream_restore == "literal"
