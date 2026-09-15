"""Property tests: encrypt streaming restore equals the batch restore.

``encrypt`` emits syntactically distinctive codec tokens, so a stream is reversed
by the codec recognizer (:class:`TokenStreamProcessor`) — the same path as
``pseudonym``/``hash`` — but with the operator's *decrypting* resolver.  The batch
reference is :meth:`EncryptOperator.deanonymize`.  This pins principle #13:
whatever the chunk framing, streaming output equals the batch output.

Unlike faker, encrypt restores *exactly* (tokens are distinctive and only our
tokens are in the mapping), so the round-trip back to the original is asserted too.
"""

from __future__ import annotations

import asyncio
import string

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from privyx.core.context import Context
from privyx.core.session import Session
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.encrypt import EncryptOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.security.keys import load_key
from privyx.streaming.deanonymizer import TokenStreamProcessor
from privyx.token.codec import FormatCodec

KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
CODEC = FormatCodec.default()

PII_POOL = [
    "alice@example.com",
    "bob.smith+tag@mail.co.uk",
    "+1-555-0142",
    "123-45-6789",
    "4111 1111 1111 1111",
    "192.168.1.254",
]

# No token delimiters in the filler, so restore back to the original is exact.
FILLER = st.text(alphabet=string.ascii_letters + string.digits + " .,!?-\n", max_size=30)
PROSE = st.lists(st.one_of(FILLER, st.sampled_from(PII_POOL)), min_size=1, max_size=8).map(" ".join)
CHUNK_SIZES = st.lists(st.integers(min_value=1, max_value=12), min_size=1, max_size=40)

SETTINGS = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    max_examples=150,
)


def chunk(text: str, sizes: list[int]) -> list[str]:
    if not text:
        return [""]
    pieces: list[str] = []
    pos = i = 0
    while pos < len(text):
        size = sizes[i % len(sizes)]
        pieces.append(text[pos : pos + size])
        pos += size
        i += 1
    return pieces


def encrypt_pseudonymize(text: str) -> tuple[str, Session, EncryptOperator]:
    """Run the real detect → policy → encrypt pipeline over ``text``."""

    async def _run() -> tuple[str, Session, EncryptOperator]:
        session = Session()
        context = Context(session_id=session.session_id)
        detection = await RegexDetector().detect(text, context)
        detection = await DefaultPolicy().decide(detection, context)
        operator = EncryptOperator(key=load_key(KEY))
        result = await operator.pseudonymize(text, detection, session, context)
        return result.text, session, operator

    return asyncio.run(_run())


def stream_deanonymize(
    chunks: list[str], operator: EncryptOperator, mapping: dict[str, str]
) -> str:
    processor = TokenStreamProcessor(CODEC, operator.build_resolver(mapping))
    return "".join([processor.feed(c) for c in chunks] + [processor.flush()])


def batch_deanonymize(text: str, operator: EncryptOperator, session: Session) -> str:
    return asyncio.run(operator.deanonymize(text, session, Context())).text


@given(text=PROSE, sizes=CHUNK_SIZES)
@SETTINGS
def test_encrypt_stream_equals_batch(text: str, sizes: list[int]) -> None:
    encrypted, session, operator = encrypt_pseudonymize(text)
    assume(session.mapping)  # uninteresting when nothing was detected

    expected = batch_deanonymize(encrypted, operator, session)
    actual = stream_deanonymize(chunk(encrypted, sizes), operator, session.mapping)

    assert actual == expected
    assert expected == text  # encrypt round-trips exactly


def test_encrypt_every_single_boundary_equals_batch() -> None:
    """Exhaustively split an encrypted multi-token string at each position."""
    text = "x alice@example.com y 123-45-6789 z 192.168.1.254 end"
    encrypted, session, operator = encrypt_pseudonymize(text)
    assert encrypted != text
    expected = batch_deanonymize(encrypted, operator, session)
    assert expected == text

    for cut in range(len(encrypted) + 1):
        actual = stream_deanonymize([encrypted[:cut], encrypted[cut:]], operator, session.mapping)
        assert actual == expected, f"cut={cut}"
    # And the finest possible chunking: one character at a time.
    assert stream_deanonymize(list(encrypted), operator, session.mapping) == expected
