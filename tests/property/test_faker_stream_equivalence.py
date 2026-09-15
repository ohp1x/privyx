"""Property tests: faker streaming restore equals the batch restore.

Faker substitutes ordinary text, so a stream is reversed by matching the exact
substituted values (:class:`StreamingDeanonymizer`, a trie) rather than the token
codec.  The batch reference is :func:`_restore_literal` — what
:meth:`FakerOperator.deanonymize` calls — so this pins the same guarantee the
token operators get in ``test_stream_equivalence``: whatever the chunk framing,
streaming output equals the batch output (principle #13).

Exact round-trip *to the original* is deliberately not asserted: a fake value is
plausible text and can, adversarially, coincide with unrelated text in the
response, so it may be restored by coincidence.  That trade-off is inherent to
realism and documented on the operator; the equivalence below is what streaming
correctness actually requires.
"""

from __future__ import annotations

import asyncio
import string

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

pytest.importorskip("faker")

from privyx.core.context import Context  # noqa: E402
from privyx.core.session import Session  # noqa: E402
from privyx.privacy.detector.builtin import RegexDetector  # noqa: E402
from privyx.privacy.operator.faker import FakerOperator, _restore_literal  # noqa: E402
from privyx.privacy.policy.default import DefaultPolicy  # noqa: E402
from privyx.streaming.deanonymizer import StreamingDeanonymizer  # noqa: E402

PII_POOL = [
    "alice@example.com",
    "bob.smith+tag@mail.co.uk",
    "+1-555-0142",
    "123-45-6789",
    "4111 1111 1111 1111",
    "192.168.1.254",
]

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


def stream_deanonymize(chunks: list[str], mapping: dict[str, str]) -> str:
    d = StreamingDeanonymizer(mapping)
    return "".join([d.feed(c) for c in chunks] + [d.flush()])


def faker_pseudonymize(text: str) -> tuple[str, Session]:
    """Run the real detect → policy → faker pipeline over ``text``."""

    async def _run() -> tuple[str, Session]:
        session = Session()
        context = Context(session_id=session.session_id)
        detection = await RegexDetector().detect(text, context)
        detection = await DefaultPolicy().decide(detection, context)
        result = await FakerOperator(seed=13).pseudonymize(text, detection, session, context)
        return result.text, session

    return asyncio.run(_run())


@given(text=PROSE, sizes=CHUNK_SIZES)
@SETTINGS
def test_faker_stream_equals_batch(text: str, sizes: list[int]) -> None:
    """Streaming restore ≡ ``FakerOperator.deanonymize`` on the whole text."""
    faked, session = faker_pseudonymize(text)
    assume(session.mapping)  # uninteresting when nothing was detected

    expected = _restore_literal(faked, session.mapping).text
    actual = stream_deanonymize(chunk(faked, sizes), session.mapping)

    assert actual == expected


@given(
    mapping=st.dictionaries(
        keys=st.text(alphabet="abc ", min_size=1, max_size=6),
        values=st.text(alphabet="XYZ", max_size=4),
        min_size=1,
        max_size=6,
    ),
    text=st.text(alphabet="abc z", max_size=40),
    sizes=CHUNK_SIZES,
)
@SETTINGS
def test_faker_adversarial_overlap_stream_equals_batch(
    mapping: dict[str, str], text: str, sizes: list[int]
) -> None:
    """With deliberately overlapping fake values, streaming ≡ batch longest-match.

    A tiny alphabet makes prefix collisions (``a``, ``ab``, ``abc``) common, which
    is exactly where a wrong hold-back buffer or a wrong longest-match would
    diverge between the two implementations.
    """
    expected = _restore_literal(text, mapping).text
    actual = stream_deanonymize(chunk(text, sizes), mapping)

    assert actual == expected
