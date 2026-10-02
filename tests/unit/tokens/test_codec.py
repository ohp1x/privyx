"""Tests for the token codec and logical token.

These pin the codec contract: encode/parse are inverses, parsing is
deterministic and rejects malformed input, the streaming primitives
behave, and the whole thing is driven by a configurable format string.
"""

from __future__ import annotations

import pytest

from privyx.token.codec import FormatCodec
from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

# Every built-in entity type, incl. the two whose names contain the default
# field separator, so ``{type}_{id}`` disambiguation is actually exercised.
ENTITY_TYPES = [
    "EMAIL",
    "PHONE",
    "SSN",
    "ZIPCODE",
    "CREDIT_CARD",
    "IP_ADDRESS",
    "API_KEY",
    "JWT",
    "PRIVATE_KEY",
    "AUTH_TOKEN",
    "URL_CREDENTIAL",
]

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


# -- tokens written without their edge literals -------------------------------


def test_restorable_finds_a_token_written_without_its_brackets() -> None:
    codec = FormatCodec.default()
    text = "mail PRIVYX_EMAIL_1 and <PRIVYX_CREDIT_CARD_2>, card PRIVYX_CREDIT_CARD_2."

    assert [(text[start:end], key) for start, end, key in codec.restorable(text)] == [
        ("PRIVYX_EMAIL_1", "<PRIVYX_EMAIL_1>"),
        ("<PRIVYX_CREDIT_CARD_2>", "<PRIVYX_CREDIT_CARD_2>"),
        ("PRIVYX_CREDIT_CARD_2", "<PRIVYX_CREDIT_CARD_2>"),
    ]


def test_restorable_takes_the_whole_bare_token_not_a_shorter_one_inside_it() -> None:
    found = FormatCodec.default().restorable("PRIVYX_EMAIL_12")

    assert [key for *_, key in found] == ["<PRIVYX_EMAIL_12>"]


@pytest.mark.parametrize(
    "text",
    ["xPRIVYX_EMAIL_1", "_PRIVYX_EMAIL_1", "PRIVYX_EMAIL_1_", "PRIVYX_EMAIL", "PRIVYX_ EMAIL_1"],
)
def test_restorable_needs_a_whole_bare_token_between_word_boundaries(text: str) -> None:
    assert list(FormatCodec.default().restorable(text)) == []


def test_restorable_at_and_prefix_len_follow_a_bare_token_as_it_grows() -> None:
    codec = FormatCodec.default()
    text = "see PRIVYX_EMAIL_1 now"

    assert codec.restorable_at(text, 4) == (18, "<PRIVYX_EMAIL_1>")
    assert codec.restorable_at(text, 5) is None  # no word boundary on its left
    # Every growing prefix reaches the end of what has arrived: it is held back.
    for end in range(5, 19):
        assert codec.restorable_prefix_len(text[:end], 4) == end - 4
    assert codec.restorable_prefix_len("see Paris", 4) == 1  # "P", then it cannot be one
    assert codec.restorable_prefix_len("xP", 1) == 0  # no word boundary on its left
    assert codec.restorable_prefix_len("see <PRIVYX_EM", 4) == 10  # a full token, as before


@pytest.mark.parametrize("fmt", ["[[{namespace}:{type}:{id}]]", "<{namespace}:{type}:{id}>"])
def test_restorable_follows_the_format(fmt: str) -> None:
    codec = FormatCodec(fmt)
    token = codec.encode(LogicalToken(namespace="PRIVYX", type="EMAIL", identifier="1"))
    text = "a PRIVYX:EMAIL:1: b"

    assert [(text[start:end], key) for start, end, key in codec.restorable(text)] == [
        ("PRIVYX:EMAIL:1", token)
    ]


@pytest.mark.parametrize(
    "fmt", ["{namespace}_{type}_{id}", "<{type}_{id}>", "<{type}:{namespace}:{id}>"]
)
def test_a_format_without_edge_literals_or_a_leading_namespace_has_no_bare_form(fmt: str) -> None:
    codec = FormatCodec(fmt)
    token = codec.encode(LogicalToken(namespace="PRIVYX", type="EMAIL", identifier="1"))
    text = f"a {token.strip('<>')} b {token} c"

    assert list(codec.restorable(text)) == [(m.start, m.end, m.text) for m in codec.finditer(text)]


# -- a token's id written on its own -------------------------------------------

ID = "9F3A1C2B7D4E5F60"
ANCHORED = f"<PRIVYX_EMAIL_{ID}>"


def test_ids_indexes_the_ids_long_enough_to_stand_alone() -> None:
    codec = FormatCodec.default()
    hashed = "<PRIVYX_SSN_ff8d9819fc0e>"

    ids = codec.ids(["<PRIVYX_EMAIL_1>", ANCHORED, hashed, "not a token"])

    assert ids is not None
    assert (ids.token(ID), ids.token("ff8d9819fc0e")) == (ANCHORED, hashed)
    assert ids.token("1") is None
    # A session of counters, however many, has no id to look for.
    assert codec.ids(["<PRIVYX_EMAIL_1>", "<PRIVYX_PHONE_12345678901>"]) is None


def test_restorable_finds_an_issued_id_written_on_its_own() -> None:
    codec = FormatCodec.default()
    text = f'{{"query": "{ID}"}} {ANCHORED} PRIVYX_EMAIL_{ID} /home/{ID}/notes'
    ids = codec.ids([ANCHORED])

    assert [(text[start:end], key) for start, end, key in codec.restorable(text, ids)] == [
        (ID, ANCHORED),
        (ANCHORED, ANCHORED),
        (f"PRIVYX_EMAIL_{ID}", ANCHORED),
        (ID, ANCHORED),
    ]
    # Without the session's ids a word is a word.
    assert [text[start:end] for start, end, _ in codec.restorable(text)] == [
        ANCHORED,
        f"PRIVYX_EMAIL_{ID}",
    ]


@pytest.mark.parametrize(
    "text",
    [f"x{ID}", f"{ID}0", f"_{ID}", f"{ID}_", ID[:-1], ID.lower(), "0123456789ABCDEF"],
)
def test_restorable_needs_the_whole_id_of_an_issued_token_between_word_boundaries(
    text: str,
) -> None:
    codec = FormatCodec.default()

    assert list(codec.restorable(text, codec.ids([ANCHORED]))) == []


def test_an_id_that_two_tokens_carry_is_not_restored_on_its_own() -> None:
    """Nothing says which of the two the id alone stands for."""
    codec = FormatCodec.default()
    other = f"<PRIVYX_PHONE_{ID}>"
    text = f"{ID} {ANCHORED} {other}"

    found = codec.restorable(text, codec.ids([ANCHORED, other]))

    assert [key for *_, key in found] == [ANCHORED, other]


def test_restorable_at_and_prefix_len_follow_an_id_as_it_grows() -> None:
    codec = FormatCodec.default()
    ids = codec.ids([ANCHORED])
    text = f"see {ID} now"

    assert codec.restorable_at(text, 4, ids) == (20, ANCHORED)
    assert codec.restorable_at(text, 5, ids) is None  # no word boundary on its left
    assert codec.restorable_at(text, 4) is None  # the session's ids were not given
    # Every growing prefix reaches the end of what has arrived: it is held back.
    for end in range(5, 21):
        assert codec.restorable_prefix_len(text[:end], 4, ids) == end - 4
        assert codec.restorable_prefix_len(text[:end], 4) == 0
    assert codec.restorable_prefix_len("see 9F4", 4, ids) == 0  # no id begins like this
    assert codec.restorable_prefix_len("x9F3A", 1, ids) == 0  # no word boundary on its left
    assert codec.restorable_prefix_len(text, 4, ids) == 0  # it has ended: nothing to wait for


@pytest.mark.parametrize(
    "fmt", ["[[{namespace}:{type}:{id}]]", "<{type}_{id}>", "{namespace}_{type}_{id}"]
)
def test_an_id_on_its_own_is_found_whatever_the_format(fmt: str) -> None:
    codec = FormatCodec(fmt)
    token = codec.encode(LogicalToken(namespace="PRIVYX", type="EMAIL", identifier=ID))

    assert list(codec.restorable(f"a {ID} b", codec.ids([token]))) == [(2, 18, token)]


def test_a_word_that_is_no_issued_id_hides_no_token_inside_it() -> None:
    """With no leading literal a token may start in the middle of a word."""
    codec = FormatCodec("{namespace}:{type}:{id}>")
    text = "123456789012PRIVYX:EMAIL:1> x"
    found = [(12, 27, "PRIVYX:EMAIL:1>")]

    assert list(codec.restorable(text)) == found
    assert list(codec.restorable(text, codec.ids([f"PRIVYX:EMAIL:{ID}>"]))) == found
