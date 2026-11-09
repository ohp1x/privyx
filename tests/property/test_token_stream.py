"""The token codec's streaming contract: stream ≡ batch at every boundary.

``stream_process([c1, ..., cN]) == batch_process(c1 + ... + cN)`` for all chunk
boundaries.  The reference is the
production batch path — :func:`privyx.privacy.operator.base.restore` — so a bug
shared by both would still surface as a wrong *value*, while these tests pin the
*equivalence*.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from privyx.core.session import Session
from privyx.privacy.operator.base import restore
from privyx.streaming.deanonymizer import TokenStreamProcessor
from privyx.token.codec import CompactCodec, FormatCodec

CODEC = FormatCodec.default()

#: Tokens this "session" has issued, with their originals.
KNOWN = {
    "<PRIVYX_EMAIL_1>": "alice@example.com",
    "<PRIVYX_CREDIT_CARD_2>": "4111 1111 1111 1111",
    "<PRIVYX_PHONE_3>": "+1-555-0142",
    "<PRIVYX_IP_ADDRESS_4>": "192.168.1.254",
    "<PRIVYX_PERSON_9F3A1C2B7D4E5F60>": "Alice Example",  # an anchor's id
}
#: Syntactically valid tokens the session never issued — must pass through intact.
UNKNOWN = ["<PRIVYX_SSN_9>", "<PRIVYX_EMAIL_deadbeef>", "<PRIVYX_SSN_0123456789ABCDEF>"]
TOKENS = [*KNOWN, *UNKNOWN]
#: The same tokens as a model sometimes writes them: without the brackets.
BARE = [token.strip("<>") for token in TOKENS]
#: Or with one bracket only, or in another case.
BROKEN = [
    *(token[:-1] for token in TOKENS),
    *(token[1:] for token in TOKENS),
    *(token.lower() for token in TOKENS),
    *(token[:-1].lower() for token in TOKENS),
]
#: Or as their id alone, and the start of one: held back in a stream, then let go.
ALONE = [*(token.strip("<>").rpartition("_")[2] for token in TOKENS), "9F3A1C2B"]

# Filler deliberately includes the delimiter characters (``<``, ``_``, ``>``) and
# a multi-byte emoji, so boundaries land inside partial/false tokens and inside
# Unicode.  Equivalence holds regardless: both paths recognize tokens identically.
FILLER = st.text(alphabet=string.ascii_letters + string.digits + " .,<>_🎉", max_size=20)
PIECES = st.lists(
    st.one_of(FILLER, st.sampled_from([*TOKENS, *BARE, *BROKEN, *ALONE])), min_size=1, max_size=12
)
CHUNK_SIZES = st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=30)

SETTINGS = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    max_examples=250,
)


def _chunk(text: str, sizes: list[int]) -> list[str]:
    if not text:
        return [""]
    pieces: list[str] = []
    pos = 0
    i = 0
    while pos < len(text):
        size = sizes[i % len(sizes)]
        pieces.append(text[pos : pos + size])
        pos += size
        i += 1
    return pieces


def _batch(text: str) -> str:
    session = Session()
    for pseudo, original in KNOWN.items():
        session.put(pseudo, original)
    return restore(text, session, CODEC).text


def _stream(chunks: list[str]) -> str:
    processor = TokenStreamProcessor(CODEC, KNOWN.get, CODEC.ids(KNOWN))
    return "".join([processor.feed(c) for c in chunks] + [processor.flush()])


@given(pieces=PIECES, sizes=CHUNK_SIZES)
@SETTINGS
def test_stream_equals_batch(pieces: list[str], sizes: list[int]) -> None:
    text = "".join(pieces)
    assert _stream(_chunk(text, sizes)) == _batch(text)


def test_every_single_boundary_equals_batch() -> None:
    """Exhaustively split a representative multi-token string at each position."""
    text = (
        "x <PRIVYX_EMAIL_1> y <PRIVYX_CREDIT_CARD_2>🎉<PRIVYX_SSN_9> z"
        " PRIVYX_PHONE_3, PRIVYX_SSN_9 PRIVYX_EMAIL_12 <PRIVYX_IP_ADDRESS_4 PRIVYX_EMAIL_1"
    )
    expected = _batch(text)
    assert expected == (
        "x alice@example.com y 4111 1111 1111 1111🎉<PRIVYX_SSN_9> z"
        " +1-555-0142, PRIVYX_SSN_9 PRIVYX_EMAIL_12 192.168.1.254 alice@example.com"
    )
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == expected, f"cut={cut}"
    # And the finest possible chunking: one character at a time.
    assert _stream(list(text)) == expected


def test_broken_tokens_of_the_session_equal_batch_at_every_boundary() -> None:
    """One bracket or another case is repaired, but only for a token the session issued."""
    text = (
        "a <PRIVYX_EMAIL_1, PRIVYX_PHONE_3> <privyx_credit_card_2> privyx_ip_address_4>"
        " <privyx_person_9f3a1c2b7d4e5f60 <PRIVYX_EMAIL_99 privyx_ssn_9> <PRIVYX_EMAIL_1"
    )
    expected = _batch(text)
    assert expected == (
        "a alice@example.com, +1-555-0142 4111 1111 1111 1111 192.168.1.254"
        " Alice Example <PRIVYX_EMAIL_99 privyx_ssn_9> alice@example.com"
    )
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == expected, f"cut={cut}"
    assert _stream(list(text)) == expected


def test_ids_written_on_their_own_equal_batch_at_every_boundary() -> None:
    """An id is a word like any other until the whole of it has arrived."""
    text = (
        '{"q": "9F3A1C2B7D4E5F60"} 9F3A1C2B 9F3A1C2B7D4E5F601 x9F3A1C2B7D4E5F60'
        " 0123456789ABCDEF 4 /home/9F3A1C2B7D4E5F60/notes <9F3A1C2B7D4E5F60"
    )
    expected = _batch(text)
    assert expected == (
        '{"q": "Alice Example"} 9F3A1C2B 9F3A1C2B7D4E5F601 x9F3A1C2B7D4E5F60'
        " 0123456789ABCDEF 4 /home/Alice Example/notes <Alice Example"
    )
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]]) == expected, f"cut={cut}"
    assert _stream(list(text)) == expected


def test_unknown_tokens_pass_through_but_known_do_not_survive() -> None:
    text = " ".join(TOKENS)
    out = _stream(list(text))
    for known in KNOWN:
        assert known not in out
    for unknown in UNKNOWN:
        assert unknown in out


def test_multi_char_delimiter_format_streams_equivalently() -> None:
    """A ``[[...]]`` syntax exercises splitting *inside* multi-char delimiters."""
    codec = FormatCodec("[[{namespace}:{type}:{id}]]", namespace="privyx")
    known = {"[[privyx:EMAIL:1]]": "alice@example.com", "[[privyx:PHONE:2]]": "+1-555-0142"}
    session = Session()
    for pseudo, original in known.items():
        session.put(pseudo, original)
    text = "a [[privyx:EMAIL:1]] mid [[privyx:PHONE:2]] then [[privyx:SSN:9]] end 🎉"
    expected = restore(text, session, codec).text

    for cut in range(len(text) + 1):
        processor = TokenStreamProcessor(codec, known.get)
        out = processor.feed(text[:cut]) + processor.feed(text[cut:]) + processor.flush()
        assert out == expected, f"cut={cut}"


def test_broken_tokens_of_a_multi_char_delimiter_format_equal_batch_at_every_boundary() -> None:
    """A closing ``]]`` split between chunks is held back whole, as is an opening ``[[``."""
    codec = FormatCodec("[[{namespace}:{type}:{id}]]", namespace="PRIVYX")
    known = {"[[PRIVYX:EMAIL:1]]": "alice@example.com", "[[PRIVYX:PHONE:2]]": "+1-555-0142"}
    session = Session()
    for pseudo, original in known.items():
        session.put(pseudo, original)
    text = (
        "a PRIVYX:EMAIL:1]] b [[PRIVYX:PHONE:2 c privyx:email:1] d [[privyx:PHONE:2]]"
        " e [PRIVYX:EMAIL:1]] f PRIVYX:SSN:9]] g [[PRIVYX:EMAIL:1]"
    )
    expected = restore(text, session, codec).text
    assert expected == (
        "a alice@example.com b +1-555-0142 c alice@example.com] d +1-555-0142"
        " e [alice@example.com f PRIVYX:SSN:9]] g alice@example.com]"
    )

    ids = codec.ids(known)
    for cut in range(len(text) + 1):
        processor = TokenStreamProcessor(codec, known.get, ids)
        out = processor.feed(text[:cut]) + processor.feed(text[cut:]) + processor.flush()
        assert out == expected, f"cut={cut}"
    processor = TokenStreamProcessor(codec, known.get, ids)
    assert "".join([processor.feed(c) for c in text] + [processor.flush()]) == expected


BRACKETED = FormatCodec("[[{namespace}:{type}:{id}]]", namespace="PRIVYX")
BRACKETED_KNOWN = {
    "[[PRIVYX:EMAIL:1]]": "alice@example.com",
    "[[PRIVYX:PERSON:9F3A1C2B7D4E5F60]]": "Alice Example",
}
BRACKETED_FORMS = [
    form
    for token in [*BRACKETED_KNOWN, "[[PRIVYX:SSN:9]]"]
    for form in (token, token[2:], token[:-2], token[1:], token[:-1], token[2:-2], token.lower())
]
BRACKETED_PIECES = st.lists(
    st.one_of(
        st.text(alphabet=string.ascii_letters + " :[]_", max_size=12),
        st.sampled_from(BRACKETED_FORMS),
    ),
    min_size=1,
    max_size=12,
)


@given(pieces=BRACKETED_PIECES, sizes=CHUNK_SIZES)
@SETTINGS
def test_multi_char_delimiter_broken_forms_stream_equals_batch(
    pieces: list[str], sizes: list[int]
) -> None:
    text = "".join(pieces)
    session = Session()
    for pseudo, original in BRACKETED_KNOWN.items():
        session.put(pseudo, original)
    processor = TokenStreamProcessor(BRACKETED, BRACKETED_KNOWN.get, BRACKETED.ids(BRACKETED_KNOWN))
    stream = "".join([processor.feed(c) for c in _chunk(text, sizes)] + [processor.flush()])
    assert stream == restore(text, session, BRACKETED).text


def test_a_flush_ends_the_text_and_what_came_before_it_is_no_context() -> None:
    """One processor restores many separate strings, each from its first character."""
    processor = TokenStreamProcessor(CODEC, KNOWN.get)

    first = processor.feed("query") + processor.flush()
    second = processor.feed("PRIVYX_EMAIL_1") + processor.flush()

    assert (first, second) == ("query", "alice@example.com")


#: A compact codec's session: tokens of both forms, as after ``token.compact``
#: was turned on mid-session.
COMPACT = CompactCodec(CODEC)
COMPACT_KNOWN = {
    **KNOWN,
    "<E~5>": "bob@example.com",
    "<CC~0123456789ABCDEF>": "5500 0000 0000 0004",  # an anchor's id
}
COMPACT_PIECES = st.lists(
    st.one_of(
        FILLER,
        st.sampled_from(
            [*TOKENS, *BARE, *ALONE, "<E~5>", "<E~6>", "<CC~0123456789ABCDEF>", "0123456789ABCDEF"]
        ),
        st.just("~"),
    ),
    min_size=1,
    max_size=12,
)


@given(pieces=COMPACT_PIECES, sizes=CHUNK_SIZES)
@SETTINGS
def test_compact_stream_equals_batch(pieces: list[str], sizes: list[int]) -> None:
    text = "".join(pieces)
    session = Session()
    for pseudo, original in COMPACT_KNOWN.items():
        session.put(pseudo, original)
    batch = restore(text, session, COMPACT).text
    processor = TokenStreamProcessor(COMPACT, COMPACT_KNOWN.get, COMPACT.ids(COMPACT_KNOWN))
    stream = "".join([processor.feed(c) for c in _chunk(text, sizes)] + [processor.flush()])
    assert stream == batch


#: Configured formats that overlap compact tokens, either way round, with a
#: session holding a token of each form.
OVERLAPS = {
    "<{type}~{id}>!": {"<EMAIL~1>!": "ana@example.com", "<E~2>": "bob@example.com"},
    "<{type}~{id}": {"<EMAIL~1": "ana@example.com", "<E~2>": "bob@example.com"},
}
OVERLAP_PIECES = st.lists(
    st.one_of(
        FILLER,
        st.sampled_from(["<EMAIL~1>!", "<EMAIL~1>", "<EMAIL~1", "<E~2>", "<E~2>!", "<E~2", "!"]),
    ),
    min_size=1,
    max_size=12,
)


@pytest.mark.parametrize("fmt", list(OVERLAPS))
@given(pieces=OVERLAP_PIECES, sizes=CHUNK_SIZES)
@SETTINGS
def test_compact_overlapping_format_stream_equals_batch(
    fmt: str, pieces: list[str], sizes: list[int]
) -> None:
    codec, known = CompactCodec(FormatCodec(fmt)), OVERLAPS[fmt]
    text = "".join(pieces)
    session = Session()
    for pseudo, original in known.items():
        session.put(pseudo, original)
    batch = restore(text, session, codec).text
    processor = TokenStreamProcessor(codec, known.get, codec.ids(known))
    stream = "".join([processor.feed(c) for c in _chunk(text, sizes)] + [processor.flush()])
    assert stream == batch
