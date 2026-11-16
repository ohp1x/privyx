"""Labeled placeholders restore the same in a stream as in a batch, at every boundary."""

from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from privyx.core.session import Session
from privyx.privacy.operator.base import restore
from privyx.streaming.deanonymizer import TokenStreamProcessor
from privyx.token.codec import LabelCodec

CODEC = LabelCodec("<{namespace}_{type}_{id}>", "<{namespace}_{type}_{id}:{label}>")
UNTYPED = LabelCodec("<{namespace}_{type}_{id}>", "[{id}|{label}]")

KNOWN = {
    "<PRIVYX_PERSON_1>": "john smith",
    "<PRIVYX_EMAIL_2>": "ana@example.com",
    "<PRIVYX_CREDIT_CARD_3>": "4111 1111 1111 1111",
    "<PRIVYX_PERSON_9F3A1C2B7D4E5F60>": "alice example",
}
#: As a model writes them: labeled, labeled with another label, plain, broken,
#: unknown, and the label alone.
PIECES_OF = [
    "<PRIVYX_PERSON_1:Alice Johnson>",
    "<PRIVYX_PERSON_1:Bob>",
    "<PRIVYX_EMAIL_2:rachel@rhodes.com>",
    "<PRIVYX_CREDIT_CARD_3:4000 0000 0000 0002>",
    "<PRIVYX_PERSON_9F3A1C2B7D4E5F60:Siti Rahayu>",
    "<PRIVYX_PERSON_7:Nobody>",
    "<PRIVYX_EMAIL_1:wrong type>",
    "<PRIVYX_PERSON_1:no end",
    "<PRIVYX_PERSON_1>",
    "PRIVYX_EMAIL_2",
    "<privyx_person_1",
    "9F3A1C2B7D4E5F60",
    "Alice Johnson",
    "[1|Alice]",
    "[3|Card]",
    "[8|x]",
]
FILLER = st.text(alphabet=string.ascii_letters + string.digits + " .:_<>[]|🎉", max_size=12)
PIECES = st.lists(st.one_of(FILLER, st.sampled_from(PIECES_OF)), min_size=1, max_size=10)
CHUNK_SIZES = st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=30)
SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=300)


def _chunk(text: str, sizes: list[int]) -> list[str]:
    pieces, pos, i = [], 0, 0
    while pos < len(text):
        pieces.append(text[pos : pos + sizes[i % len(sizes)]])
        pos += sizes[i % len(sizes)]
        i += 1
    return pieces or [""]


def _batch(text: str, codec: LabelCodec) -> str:
    session = Session()
    for token, value in KNOWN.items():
        session.put(token, value)
    return restore(text, session, codec).text


def _stream(chunks: list[str], codec: LabelCodec) -> str:
    processor = TokenStreamProcessor(codec, KNOWN.get, codec.ids(KNOWN))
    return "".join([processor.feed(chunk) for chunk in chunks] + [processor.flush()])


@given(pieces=PIECES, sizes=CHUNK_SIZES)
@SETTINGS
def test_stream_equals_batch(pieces: list[str], sizes: list[int]) -> None:
    text = "".join(pieces)
    for codec in (CODEC, UNTYPED):
        assert _stream(_chunk(text, sizes), codec) == _batch(text, codec)


def test_every_single_boundary_equals_batch() -> None:
    text = " ".join(PIECES_OF)
    expected = _batch(text, CODEC)
    assert expected.startswith("john smith john smith ana@example.com 4111 1111 1111 1111")
    assert "Alice Johnson [1|Alice]" in expected  # the label alone is never restored
    assert "<PRIVYX_PERSON_7:Nobody> <PRIVYX_EMAIL_1:wrong type>" in expected
    for cut in range(len(text) + 1):
        assert _stream([text[:cut], text[cut:]], CODEC) == expected, f"cut={cut}"
    assert _stream(list(text), CODEC) == expected
