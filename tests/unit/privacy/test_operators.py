"""Tests for the operators and the anchor that feeds them.

The anchor path is the reason these exist: ``anchor.secret`` is config that
reaches the operator through the builder, and without a test that goes through
``build_operator`` the wiring can be removed without anything failing.
"""

from __future__ import annotations

import pytest

from privyx.config.schema import Settings
from privyx.core.builder import build_anchor, build_operator
from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.core.result import Detection, Span
from privyx.core.session import Session
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.operator.hash import HashOperator
from privyx.privacy.operator.pseudonym import ANCHOR_TOKEN_LENGTH, PseudonymOperator
from privyx.privacy.operator.redact import RedactOperator
from privyx.token.codec import FormatCodec

TEXT = "Email alice@example.com or bob@example.com, card 4111 1111 1111 1111"


def _detection() -> Detection:
    """Spans for ``TEXT``, hand-written so these tests do not depend on the detector."""
    return Detection(
        spans=[
            Span(TEXT.index("alice@example.com"), TEXT.index("alice@example.com") + 17, "EMAIL",
                 "alice@example.com"),
            Span(TEXT.index("bob@example.com"), TEXT.index("bob@example.com") + 15, "EMAIL",
                 "bob@example.com"),
            Span(TEXT.index("4111"), len(TEXT), "CREDIT_CARD", "4111 1111 1111 1111"),
        ]
    )


# --- counters ---------------------------------------------------------------


async def test_counters_read_left_to_right() -> None:
    result = await PseudonymOperator().pseudonymize(TEXT, _detection(), Session(), Context())
    assert result.text == (
        "Email <PRIVYX_EMAIL_1> or <PRIVYX_EMAIL_2>, card <PRIVYX_CREDIT_CARD_3>"
    )


async def test_same_value_reuses_its_pseudonym() -> None:
    text = "a@b.com and a@b.com"
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com"), Span(12, 19, "EMAIL", "a@b.com")])
    result = await PseudonymOperator().pseudonymize(text, detection, Session(), Context())
    assert result.text == "<PRIVYX_EMAIL_1> and <PRIVYX_EMAIL_1>"


async def test_counter_skips_ids_already_taken() -> None:
    """A deleted mapping entry makes ``len()`` go backwards; the probe must not collide."""
    session = Session()
    session.put("<PRIVYX_EMAIL_1>", "taken@example.com")
    session.put("<PRIVYX_EMAIL_2>", "also@example.com")
    del session.mapping["<PRIVYX_EMAIL_1>"]

    detection = Detection(spans=[Span(0, 7, "EMAIL", "new@x.c")])
    result = await PseudonymOperator().pseudonymize("new@x.c", detection, session, Context())
    assert result.text != "<PRIVYX_EMAIL_2>"
    assert session.mapping["<PRIVYX_EMAIL_2>"] == "also@example.com"


# --- anchors ----------------------------------------------------------------


async def test_anchored_pseudonyms_are_stable_across_sessions() -> None:
    operator = PseudonymOperator(anchor=HMACAnchor("s3cret"))
    first = await operator.pseudonymize(TEXT, _detection(), Session(), Context())
    second = await operator.pseudonymize(TEXT, _detection(), Session(), Context())
    assert first.text == second.text
    assert "<PRIVYX_EMAIL_1>" not in first.text


async def test_anchored_pseudonyms_depend_on_the_secret() -> None:
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    one = await PseudonymOperator(anchor=HMACAnchor("key-one")).pseudonymize(
        "a@b.com", detection, Session(), Context()
    )
    two = await PseudonymOperator(anchor=HMACAnchor("key-two")).pseudonymize(
        "a@b.com", detection, Session(), Context()
    )
    assert one.text != two.text


async def test_anchored_pseudonyms_round_trip() -> None:
    """An anchor changes how pseudonyms are named, not whether they reverse."""
    operator = PseudonymOperator(anchor=HMACAnchor("s3cret"))
    session = Session()
    result = await operator.pseudonymize(TEXT, _detection(), session, Context())
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == TEXT


async def test_anchor_token_shape() -> None:
    operator = PseudonymOperator(anchor=HMACAnchor("s3cret"))
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    result = await operator.pseudonymize("a@b.com", detection, Session(), Context())
    token = result.text.removeprefix("<PRIVYX_EMAIL_").removesuffix(">")
    assert len(token) == ANCHOR_TOKEN_LENGTH
    assert token == token.upper()
    assert all(c in "0123456789ABCDEF" for c in token)


async def test_distinct_values_get_distinct_anchors() -> None:
    result = await PseudonymOperator(anchor=HMACAnchor("s3cret")).pseudonymize(
        TEXT, _detection(), Session(), Context()
    )
    pseudonyms = [w for w in result.text.split() if w.startswith("<PRIVYX_")]
    assert len(set(pseudonyms)) == len(pseudonyms)


# --- builder wiring ---------------------------------------------------------


def test_anchor_is_off_without_a_secret() -> None:
    """An empty secret means no anchoring, not anchoring with a guessable key."""
    assert build_anchor(Settings()) is None


def test_anchor_is_built_when_a_secret_is_set() -> None:
    settings = Settings.from_dict({"anchor": {"type": "hmac", "secret": "s3cret"}})
    assert isinstance(build_anchor(settings), HMACAnchor)


def test_unimplemented_anchor_type_fails_at_startup() -> None:
    settings = Settings.from_dict({"anchor": {"type": "pasp", "secret": "s3cret"}})
    with pytest.raises(ConfigError, match="pasp"):
        build_anchor(settings)


def test_unknown_anchor_type_fails_at_startup() -> None:
    settings = Settings.from_dict({"anchor": {"type": "nope", "secret": "s3cret"}})
    with pytest.raises(ConfigError, match="nope"):
        build_anchor(settings)


async def test_configured_secret_reaches_the_operator() -> None:
    """The end-to-end claim: setting ``anchor.secret`` changes the output."""
    settings = Settings.from_dict({"anchor": {"type": "hmac", "secret": "s3cret"}})
    operator = build_operator(settings.operator.model_dump(), anchor=build_anchor(settings))
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    result = await operator.pseudonymize("a@b.com", detection, Session(), Context())
    assert result.text != "<PRIVYX_EMAIL_1>"


async def test_default_config_still_uses_counters() -> None:
    settings = Settings()
    operator = build_operator(settings.operator.model_dump(), anchor=build_anchor(settings))
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    result = await operator.pseudonymize("a@b.com", detection, Session(), Context())
    assert result.text == "<PRIVYX_EMAIL_1>"


def test_operators_that_cannot_anchor_ignore_it() -> None:
    """An anchor must not break operators whose factories do not accept one."""
    operator = build_operator({"type": "redact"}, anchor=HMACAnchor("s3cret"))
    assert isinstance(operator, RedactOperator)


def test_anchor_is_not_reachable_from_yaml() -> None:
    """The anchor is passed as an object; a config key of the same name is inert."""
    operator = build_operator({"type": "pseudonym", "anchor": "not-an-anchor"})
    assert isinstance(operator, PseudonymOperator)


# --- hash operator ----------------------------------------------------------


async def test_hash_operator_round_trips() -> None:
    """Regression: deanonymize used the ``<PRIVYX_...>`` pattern and never matched."""
    operator = HashOperator()
    session = Session()
    result = await operator.pseudonymize(TEXT, _detection(), session, Context())
    assert "alice@example.com" not in result.text
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == TEXT


async def test_hash_operator_leaves_unknown_tokens_alone() -> None:
    """A syntactically valid token the session never issued is passed through."""
    unknown = "<PRIVYX_EMAIL_deadbeef>"
    result = await HashOperator().deanonymize(unknown, Session(), Context())
    assert result.text == unknown
    assert result.transformations == []


async def test_hash_operator_honours_its_configured_length() -> None:
    operator = build_operator({"type": "hash", "length": 8})
    session = Session()
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    result = await operator.pseudonymize("a@b.com", detection, session, Context())
    token = FormatCodec.default().parse(result.text)
    assert token is not None
    assert len(token.identifier) == 8  # the digest was truncated to `length`
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == "a@b.com"


# --- redaction --------------------------------------------------------------


async def test_redaction_is_irreversible() -> None:
    session = Session()
    operator = RedactOperator()
    result = await operator.pseudonymize(TEXT, _detection(), session, Context())
    assert "alice@example.com" not in result.text
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == result.text
