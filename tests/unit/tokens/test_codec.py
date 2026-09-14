"""Tests for the token codec and logical token.

These pin the contract in ``.temp/token-system.md``: encode/parse are inverses,
parsing is deterministic and rejects malformed input, the streaming primitives
behave, and the whole thing is driven by a configurable format string.
"""

from __future__ import annotations

import pytest

from privyx.token.codec import FormatCodec
from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

# Every built-in entity type, incl. the two whose names contain the default
# field separator, so ``{type}_{id}`` disambiguation is actually exercised.
ENTITY_TYPES = ["EMAIL", "PHONE", "SSN", "ZIPCODE", "CREDIT_CARD", "IP_ADDRESS"]

# The three identifier shapes the operators produce.
IDENTIFIERS = ["1", "42", "9F3A1C2B7D4E5F60", "fb98d44a"]


# --- encoding / parsing / round trip ----------------------------------------


@pytest.mark.parametrize("entity", ENTITY_TYPES)
@pytest.mark.parametrize("identifier", IDENTIFIERS)
def test_round_trip(entity: str, identifier: str) -> None:
    codec = FormatCodec.default()
    token = LogicalToken("PRIVYX", entity, identifier)
    assert codec.parse(codec.encode(token)) == token


def test_encode_matches_legacy_syntax() -> None:
    """The default format reproduces the historical ``<PRIVYX_TYPE_ID>`` output."""
    codec = FormatCodec.default()
    assert codec.encode(LogicalToken("PRIVYX", "EMAIL", "1")) == "<PRIVYX_EMAIL_1>"
    assert codec.encode(LogicalToken("PRIVYX", "CREDIT_CARD", "2")) == "<PRIVYX_CREDIT_CARD_2>"


def test_parse_rejects_non_token() -> None:
    codec = FormatCodec.default()
    assert codec.parse("just some text") is None


# --- finding tokens in text -------------------------------------------------


def test_finditer_locates_every_token_between_prose() -> None:
    codec = FormatCodec.default()
    text = "a <PRIVYX_EMAIL_1> b <PRIVYX_CREDIT_CARD_2> c"
    matches = list(codec.finditer(text))
    assert [(m.text, m.token.type, m.token.identifier) for m in matches] == [
        ("<PRIVYX_EMAIL_1>", "EMAIL", "1"),
        ("<PRIVYX_CREDIT_CARD_2>", "CREDIT_CARD", "2"),
    ]
    for m in matches:
        assert text[m.start : m.end] == m.text


def test_finditer_handles_adjacent_tokens() -> None:
    codec = FormatCodec.default()
    text = "<PRIVYX_EMAIL_1><PRIVYX_PHONE_2>"
    assert [m.text for m in codec.finditer(text)] == [
        "<PRIVYX_EMAIL_1>",
        "<PRIVYX_PHONE_2>",
    ]


def test_finditer_ignores_token_like_but_invalid_text() -> None:
    codec = FormatCodec.default()
    # Missing closing delimiter, empty field, and a stray opener: none are tokens.
    assert list(codec.finditer("<PRIVYX_EMAIL_1 and <PRIVYX__2> and < >")) == []


# --- malformed input --------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "<PRIVYX_EMAIL_1",  # unclosed
        "PRIVYX_EMAIL_1>",  # no opener
        "<PRIVYX_EMAIL_>",  # empty id
        "<PRIVYX__1>",  # empty type
        "<_EMAIL_1>",  # empty namespace
        "<PRIVYX_EMAIL_1_>",  # trailing separator, no id
        "<PRIVYX-EMAIL-1>",  # wrong delimiter
        "<privyx_em ail_1>",  # whitespace inside
    ],
)
def test_parse_returns_none_for_malformed(bad: str) -> None:
    assert FormatCodec.default().parse(bad) is None


def test_over_long_identifier_is_not_a_token() -> None:
    """Field caps bound the parser (spec §11); an oversized id is not accepted."""
    codec = FormatCodec.default()
    huge = "a" * 65  # id grammar caps at 64
    assert codec.parse(f"<PRIVYX_EMAIL_{huge}>") is None


# --- streaming primitives ---------------------------------------------------


def test_longest_prefix_len_tracks_a_growing_token() -> None:
    codec = FormatCodec.default()
    token = "<PRIVYX_EMAIL_1>"
    # Every proper prefix is a viable token prefix.
    for k in range(1, len(token)):
        assert codec.longest_prefix_len(token[:k], 0) == k
    # A character that cannot begin a token yields 0.
    assert codec.longest_prefix_len("hello", 0) == 0


def test_match_at_returns_complete_token_only() -> None:
    codec = FormatCodec.default()
    assert codec.match_at("<PRIVYX_EMAIL_1> rest", 0).end == 16  # type: ignore[union-attr]
    assert codec.match_at("<PRIVYX_EMAIL_1", 0) is None  # incomplete
    assert codec.match_at("x<PRIVYX_EMAIL_1>", 0) is None  # not at pos


# --- format validation ------------------------------------------------------


@pytest.mark.parametrize(
    "fmt",
    [
        "<{type}{id}>",  # adjacent fields, ambiguous
        "<{namespace}_{type}>",  # missing {id}
        "<{id}>",  # missing {type}
        "<{unknown}_{type}_{id}>",  # unknown field
        "<{type}_{id}}>",  # stray brace
        "",  # empty
    ],
)
def test_bad_format_raises(fmt: str) -> None:
    with pytest.raises(TokenFormatError):
        FormatCodec(fmt)


def test_encode_rejects_unrepresentable_field() -> None:
    codec = FormatCodec.default()
    # A hyphen is not in the id grammar; encoding must refuse rather than emit an
    # unparseable token (spec §11, injection through token fields).
    with pytest.raises(TokenFormatError):
        codec.encode(LogicalToken("PRIVYX", "EMAIL", "a-b"))


# --- logical token validation -----------------------------------------------


@pytest.mark.parametrize("field", ["namespace", "type", "identifier"])
def test_logical_token_rejects_empty_field(field: str) -> None:
    kwargs = {"namespace": "PRIVYX", "type": "EMAIL", "identifier": "1", field: ""}
    with pytest.raises(ValueError, match=field):
        LogicalToken(**kwargs)


def test_logical_token_rejects_control_characters() -> None:
    with pytest.raises(ValueError):
        LogicalToken("PRIVYX", "EMAIL", "1\n2")


# --- configurable syntax (the whole point) ----------------------------------


@pytest.mark.parametrize(
    "fmt, expected",
    [
        ("[[{namespace}:{type}:{id}]]", "[[privyx:EMAIL:1]]"),
        ("<{namespace}:{type}:{id}>", "<privyx:EMAIL:1>"),
        ("=[{type}|{id}]=", "=[EMAIL|1]="),  # no namespace field
    ],
)
def test_custom_format_round_trips(fmt: str, expected: str) -> None:
    codec = FormatCodec(fmt, namespace="privyx")
    token = LogicalToken("privyx", "EMAIL", "1")
    encoded = codec.encode(token)
    assert encoded == expected
    parsed = codec.parse(encoded)
    assert parsed is not None
    assert (parsed.type, parsed.identifier) == ("EMAIL", "1")


def test_custom_format_finds_tokens_in_text() -> None:
    codec = FormatCodec("[[{namespace}:{type}:{id}]]", namespace="privyx")
    text = "call [[privyx:PHONE:7]] now"
    assert [m.text for m in codec.finditer(text)] == ["[[privyx:PHONE:7]]"]
