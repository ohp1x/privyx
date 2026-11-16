"""Labeled placeholders (``token.label_format``): written, found, and restored by id."""

from __future__ import annotations

import time

import pytest

from privyx.core.errors import ConfigError
from privyx.core.session import Session
from privyx.privacy.operator.base import restore
from privyx.token.codec import LabelCodec
from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

FORMAT = "<{namespace}_{type}_{id}>"
LABELED = "<{namespace}_{type}_{id}:{label}>"
CODEC = LabelCodec(FORMAT, LABELED)
PERSON = LogicalToken("PRIVYX", "PERSON", "1")


def _restore(text: str, mapping: dict[str, str], codec: LabelCodec = CODEC) -> str:
    session = Session()
    for token, value in mapping.items():
        session.put(token, value)
    return restore(text, session, codec).text


def test_a_token_is_written_with_its_label() -> None:
    assert CODEC.label(PERSON, "Alice Johnson") == "<PRIVYX_PERSON_1:Alice Johnson>"
    assert CODEC.encode(PERSON) == "<PRIVYX_PERSON_1>"  # what the session maps


@pytest.mark.parametrize(
    "label", ["Ali>ce", "two\nlines", 'a "quote"', "back\\slash", "x" * 65, ""]
)
def test_a_label_that_cannot_be_written_there_is_refused(label: str) -> None:
    assert CODEC.label(PERSON, label) is None


@pytest.mark.parametrize(
    "label_format",
    [
        "<{namespace}_{type}_{id}>",  # no {label}
        "<{namespace}_{type}:{label}>",  # no {id}
        "{label}:<{id}>",  # {label} first
        "<{id}:{label}",  # {label} last
        "<{id}{label}>",  # no delimiter between fields
        "<{id}:{name}>",  # an unknown field
    ],
)
def test_an_unusable_label_format_is_refused(label_format: str) -> None:
    with pytest.raises(TokenFormatError):
        LabelCodec(FORMAT, label_format)
    assert issubclass(TokenFormatError, ConfigError)


def test_a_labeled_placeholder_restores_by_its_id_and_the_label_alone_never() -> None:
    mapping = {"<PRIVYX_PERSON_1>": "john smith"}
    text = "<PRIVYX_PERSON_1:Alice Johnson> read Alice in Wonderland; Alice Johnson agreed"
    assert _restore(text, mapping) == "john smith read Alice in Wonderland; Alice Johnson agreed"


def test_a_label_the_model_changed_still_restores() -> None:
    mapping = {"<PRIVYX_PERSON_1>": "john smith"}
    assert _restore("<PRIVYX_PERSON_1:Alicia J.>", mapping) == "john smith"


def test_the_labeled_form_wins_over_the_bare_token_at_the_same_position() -> None:
    # The bare form alone would take `<PRIVYX_PERSON_1` and leave `:Alice Johnson>`.
    mapping = {"<PRIVYX_PERSON_1>": "john smith"}
    assert list(CODEC.restorable("<PRIVYX_PERSON_1:Alice Johnson>", CODEC.ids(mapping))) == [
        (0, 31, "<PRIVYX_PERSON_1>")
    ]
    assert CODEC.restorable_at("<PRIVYX_PERSON_1:Alice Johnson>", 0, CODEC.ids(mapping)) == (
        31,
        "<PRIVYX_PERSON_1>",
    )


def test_an_unknown_or_ambiguous_labeled_placeholder_is_left_as_written() -> None:
    # Without {type}, the id alone points to two tokens here.
    codec = LabelCodec(FORMAT, "<{namespace}_{id}:{label}>")
    mapping = {"<PRIVYX_PERSON_1>": "john smith", "<PRIVYX_EMAIL_1>": "ana@example.com"}
    text = "<PRIVYX_1:Alice> and <PRIVYX_PERSON_7:Bob Lee> and <PRIVYX_2:Carol>"
    assert _restore(text, mapping, codec) == text
    assert _restore(text, mapping, CODEC) == text


def test_a_format_without_type_finds_the_token_by_its_id() -> None:
    codec = LabelCodec(FORMAT, "<{namespace}_{id}:{label}>")
    mapping = {"<PRIVYX_PERSON_1>": "john smith", "<PRIVYX_EMAIL_2>": "ana@example.com"}
    assert _restore("<PRIVYX_1:Alice> wrote <PRIVYX_2:bob@example.org>", mapping, codec) == (
        "john smith wrote ana@example.com"
    )


def test_the_type_and_namespace_must_match_when_the_format_has_them() -> None:
    mapping = {"<PRIVYX_PERSON_1>": "john smith"}
    assert _restore("<PRIVYX_EMAIL_1:Alice>", mapping) == "<PRIVYX_EMAIL_1:Alice>"
    assert _restore("<OTHER_PERSON_1:Alice>", mapping) == "<OTHER_PERSON_1:Alice>"


def test_a_unicode_label_round_trips() -> None:
    label = "Siti Nurhaliza Ñúñez 李"
    written = CODEC.label(PERSON, label)
    assert written == f"<PRIVYX_PERSON_1:{label}>"
    assert _restore(f"hi {written}!", {"<PRIVYX_PERSON_1>": "john smith"}) == "hi john smith!"


def test_plain_tokens_and_their_forms_still_restore() -> None:
    mapping = {"<PRIVYX_PERSON_1>": "john smith", "<PRIVYX_EMAIL_2>": "ana@example.com"}
    text = "<PRIVYX_PERSON_1>, PRIVYX_EMAIL_2 and <privyx_person_1"
    assert _restore(text, mapping) == "john smith, ana@example.com and john smith"


def test_finditer_finds_labeled_and_plain_placeholders() -> None:
    text = "a <PRIVYX_PERSON_1:Alice> b <PRIVYX_EMAIL_2> c"
    assert [match.text for match in CODEC.finditer(text)] == [
        "<PRIVYX_PERSON_1:Alice>",
        "<PRIVYX_EMAIL_2>",
    ]


def test_a_labeled_placeholder_is_held_back_until_it_ends() -> None:
    ids = CODEC.ids({"<PRIVYX_PERSON_1>": "john smith"})
    for end in range(1, len("<PRIVYX_PERSON_1:Alice Johnson")):
        text = "<PRIVYX_PERSON_1:Alice Johnson"[:end]
        assert CODEC.restorable_prefix_len(text, 0, ids) == len(text)
    assert CODEC.start_chars(ids) is not None and "<" in CODEC.start_chars(ids)  # type: ignore[operator]


@pytest.mark.parametrize(
    "shape",
    [
        "<PRIVYX_PERSON_1:" + "a" * 100_000,
        "<PRIVYX_" * 20_000,
        "<PRIVYX_A_" + "A_" * 50_000,
        "<P_A_1:" * 20_000,
        ":" * 100_000,
        "<PRIVYX_PERSON_1:" + "a " * 50_000 + ">",
    ],
)
def test_the_label_codec_stays_linear_on_adversarial_input(shape: str) -> None:
    ids = CODEC.ids({"<PRIVYX_PERSON_1>": "john smith"})
    start = time.perf_counter()
    list(CODEC.restorable(shape, ids))
    for pos in range(0, len(shape), 97):
        CODEC.restorable_at(shape, pos, ids)
        CODEC.restorable_prefix_len(shape, pos, ids)
    assert time.perf_counter() - start < 2.0
