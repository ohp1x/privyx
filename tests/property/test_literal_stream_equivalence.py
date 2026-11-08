"""Property tests: ``partial`` and ``fpe`` restore the same in a stream as in a batch.

Both substitute ordinary text, like ``faker``, so a stream is reversed by
matching the exact substitutes (:class:`StreamingDeanonymizer`).  Whatever the
chunk framing, that must equal the operator's own batch restore, and, since
their substitutes do not occur in the filler, give back the original text.
"""

from __future__ import annotations

import asyncio
import string

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from privyx.core.context import Context
from privyx.core.session import Session
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.faker import LiteralOperator
from privyx.privacy.operator.fpe import FPEOperator
from privyx.privacy.operator.partial import PartialOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.streaming.deanonymizer import StreamingDeanonymizer

PII_POOL = [
    "alice@example.com",
    "bob.smith+tag@mail.co.uk",
    "+1-555-0142",
    "123-45-6789",
    "4111 1111 1111 1111",
    "192.168.1.254",
]

FILLER = st.text(alphabet=string.ascii_letters + " .,!?\n", max_size=30)
PROSE = st.lists(st.one_of(FILLER, st.sampled_from(PII_POOL)), min_size=1, max_size=8).map(" ".join)
CHUNK_SIZES = st.lists(st.integers(min_value=1, max_value=12), min_size=1, max_size=40)

SETTINGS = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    max_examples=150,
)


def _chunk(text: str, sizes: list[int]) -> list[str]:
    pieces, pos, i = [], 0, 0
    while pos < len(text):
        pieces.append(text[pos : pos + sizes[i % len(sizes)]])
        pos += sizes[i % len(sizes)]
        i += 1
    return pieces or [""]


def _mask(operator: LiteralOperator, text: str) -> tuple[str, Session]:
    async def run() -> tuple[str, Session]:
        session, context = Session(), Context()
        detection = await RegexDetector().detect(text, context)
        detection = await DefaultPolicy().decide(detection, context)
        return (await operator.pseudonymize(text, detection, session, context)).text, session

    return asyncio.run(run())


@pytest.mark.parametrize(
    "operator", [PartialOperator(), FPEOperator(seed=13)], ids=["partial", "fpe"]
)
@given(text=PROSE, sizes=CHUNK_SIZES)
@SETTINGS
def test_stream_equals_batch_and_the_original(
    operator: LiteralOperator, text: str, sizes: list[int]
) -> None:
    masked, session = _mask(operator, text)
    assume(session.mapping)

    batch = asyncio.run(operator.deanonymize(masked, session, Context())).text
    stream = StreamingDeanonymizer(session.mapping)
    streamed = "".join([stream.feed(c) for c in _chunk(masked, sizes)] + [stream.flush()])

    assert streamed == batch == text
