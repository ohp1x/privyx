"""Property tests: streaming output equals batch output.

Principle #13 — every streaming algorithm is property-tested against random
chunk boundaries.  The reference here is deliberately *not* the streaming
implementation run with a different split: comparing an algorithm to itself
proves self-consistency, not correctness.  The reference is
:meth:`PseudonymOperator.deanonymize`, the batch implementation the streaming
path is supposed to be equivalent to.

Three properties are covered:

* ``feed``/``flush`` over any chunking ≡ ``Operator.deanonymize`` on the whole.
* Pseudonymize → stream-deanonymize round-trips to the original text.
* :class:`SSEDecoder` yields the same events for any framing of the same bytes
  — the invariant whose absence let pseudonyms leak through the proxy.

Each pseudonym property runs over both suffix styles.  Anchored pseudonyms end
in a hex token rather than a counter, which changes where prefixes collide in
the trie, so testing only the default would leave half the placeholder grammar
unexercised.
"""

from __future__ import annotations

import asyncio
import json
import string

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from privyx.core.context import Context
from privyx.core.result import Detection, Span
from privyx.core.session import Session
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.streaming.deanonymizer import StreamingDeanonymizer
from privyx.streaming.sse import SSEDecoder, SSEEvent, parse_sse

# Realistic PII, one per entity type the built-in detector knows, so the
# generated corpus exercises multi-word entity names (CREDIT_CARD, IP_ADDRESS)
# whose pseudonyms contain underscores.
PII_POOL = [
    "alice@example.com",
    "bob.smith+tag@mail.co.uk",
    "+1-555-0142",
    "(021) 555-0199",
    "123-45-6789",
    "4111 1111 1111 1111",
    "192.168.1.254",
]

# Filler excludes '<' so it can never synthesise a pseudonym by accident; the
# point of these tests is the algorithm, not the detector's precision.
FILLER = st.text(alphabet=string.ascii_letters + string.digits + " .,!?-\n", max_size=30)

PROSE = st.lists(st.one_of(FILLER, st.sampled_from(PII_POOL)), min_size=1, max_size=8).map(
    " ".join
)

# Chunk sizes, not offsets: a list of small positive ints slices the text into
# pieces whose boundaries land inside pseudonyms often enough to matter.
CHUNK_SIZES = st.lists(st.integers(min_value=1, max_value=12), min_size=1, max_size=40)

SETTINGS = settings(
    deadline=None,  # each example spins an event loop
    suppress_health_check=[HealthCheck.too_slow],
    max_examples=150,
)


def chunk(text: str, sizes: list[int]) -> list[str]:
    """Split ``text`` into pieces of the given sizes, cycling if they run out."""
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


#: The two pseudonym suffix styles: per-session counters, and anchor-derived
#: hex tokens.  Parametrizing on this is what keeps principle #13 honest for
#: both — the trie sees a different prefix structure in each case.
ANCHORS = [None, HMACAnchor("property-test-secret")]


def operator(anchor: HMACAnchor | None) -> PseudonymOperator:
    return PseudonymOperator(anchor=anchor)


def pseudonymize(text: str, anchor: HMACAnchor | None = None) -> tuple[str, Session]:
    """Run the real detect → policy → pseudonymize pipeline over ``text``."""

    async def _run() -> tuple[str, Session]:
        session = Session()
        context = Context(session_id=session.session_id)
        detection = await RegexDetector().detect(text, context)
        detection = await DefaultPolicy().decide(detection, context)
        result = await operator(anchor).pseudonymize(text, detection, session, context)
        return result.text, session

    return asyncio.run(_run())


def batch_deanonymize(text: str, session: Session, anchor: HMACAnchor | None = None) -> str:
    """The reference implementation the streaming path must match."""

    async def _run() -> str:
        result = await operator(anchor).deanonymize(
            text, session, Context(session_id=session.session_id)
        )
        return result.text

    return asyncio.run(_run())


def stream_deanonymize(chunks: list[str], session: Session) -> str:
    d = StreamingDeanonymizer(session.mapping)
    return "".join([d.feed(c) for c in chunks] + [d.flush()])


# --------------------------------------------------------------------------


@pytest.mark.parametrize("anchor", ANCHORS, ids=["counter", "anchored"])
@given(text=PROSE, sizes=CHUNK_SIZES)
@SETTINGS
def test_stream_equals_batch_operator(
    anchor: HMACAnchor | None, text: str, sizes: list[int]
) -> None:
    """Streaming deanonymization ≡ ``PseudonymOperator.deanonymize``.

    The comparison is against the batch operator, so a bug shared by every
    streaming code path — a wrong trie, a dropped flush — still fails.
    """
    pseudonymized, session = pseudonymize(text, anchor)
    assume(session.mapping)  # uninteresting when nothing was detected

    expected = batch_deanonymize(pseudonymized, session, anchor)
    actual = stream_deanonymize(chunk(pseudonymized, sizes), session)

    assert actual == expected


@pytest.mark.parametrize("anchor", ANCHORS, ids=["counter", "anchored"])
@given(text=PROSE, sizes=CHUNK_SIZES)
@SETTINGS
def test_round_trip_through_pipeline(
    anchor: HMACAnchor | None, text: str, sizes: list[int]
) -> None:
    """Pseudonymize → stream-deanonymize returns the original text exactly."""
    pseudonymized, session = pseudonymize(text, anchor)

    restored = stream_deanonymize(chunk(pseudonymized, sizes), session)

    assert restored == text


@pytest.mark.parametrize("anchor", ANCHORS, ids=["counter", "anchored"])
@given(text=PROSE, sizes=CHUNK_SIZES)
@SETTINGS
def test_no_pseudonym_survives_streaming(
    anchor: HMACAnchor | None, text: str, sizes: list[int]
) -> None:
    """No pseudonym issued for this session may appear in the streamed output.

    This is the leak check stated as a property: whatever the framing, the
    client never sees a placeholder.
    """
    pseudonymized, session = pseudonymize(text, anchor)
    assume(session.mapping)

    out = stream_deanonymize(chunk(pseudonymized, sizes), session)

    for pseudonym in session.mapping:
        assert pseudonym not in out


@given(text=PROSE)
@SETTINGS
def test_anchored_pseudonyms_are_session_independent(text: str) -> None:
    """The anchor's whole purpose: the same value pseudonymizes identically.

    Two independent sessions, no shared state — only the key relates them.
    """
    anchor = HMACAnchor("property-test-secret")
    first, first_session = pseudonymize(text, anchor)
    second, _ = pseudonymize(text, anchor)
    assume(first_session.mapping)

    assert first == second


@given(
    mapping=st.dictionaries(
        keys=st.text(alphabet="AB_<>", min_size=1, max_size=6),
        values=st.text(alphabet="xy", max_size=4),
        min_size=1,
        max_size=6,
    ),
    text=st.text(alphabet="AB_<>z", max_size=40),
    sizes=CHUNK_SIZES,
)
@SETTINGS
def test_adversarial_prefixes_match_longest(
    mapping: dict[str, str], text: str, sizes: list[int]
) -> None:
    """With deliberately overlapping keys, streaming ≡ single-chunk longest-match.

    A tiny alphabet makes prefix collisions (``A``, ``AB``, ``AB_``) the common
    case rather than a rarity.  Here the reference is the same algorithm fed in
    one chunk — the batch operator only recognises well-formed ``<PRIVYX_…>``
    placeholders, so it cannot arbitrate these — but the split/whole comparison
    is still what pins the hold-back buffer's behaviour.
    """
    expected = stream_deanonymize([text], mapping_session(mapping))
    actual = stream_deanonymize(chunk(text, sizes), mapping_session(mapping))

    assert actual == expected


def mapping_session(mapping: dict[str, str]) -> Session:
    session = Session()
    for pseudo, original in mapping.items():
        session.put(pseudo, original)
    return session


# --------------------------------------------------------------------------
# SSE framing


@given(
    deltas=st.lists(st.text(alphabet=string.ascii_letters + " ", max_size=12), max_size=6),
    sizes=CHUNK_SIZES,
)
@SETTINGS
def test_sse_decoder_is_framing_invariant(deltas: list[str], sizes: list[int]) -> None:
    """The decoder yields the same events however the bytes are cut up.

    The stateless parser fails this: given a body split into pieces that do not
    each end on a blank line, it drops every event it cannot terminate.
    """
    body = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': d}}]})}\n\n" for d in deltas
    )

    whole = SSEDecoder()
    expected = [ev.data for ev in whole.feed(body) + whole.flush()]

    split = SSEDecoder()
    actual: list[str] = []
    for piece in chunk(body, sizes):
        actual.extend(ev.data for ev in split.feed(piece))
    actual.extend(ev.data for ev in split.flush())

    assert actual == expected


@given(deltas=st.lists(st.text(alphabet=string.ascii_letters + " ", max_size=12), max_size=6))
@SETTINGS
def test_sse_event_round_trips_through_decoder(deltas: list[str]) -> None:
    """Serializing an event and decoding it returns the same payload."""
    events = [SSEEvent(data=d, event="delta") for d in deltas]
    body = "".join(ev.to_str() for ev in events)

    decoder = SSEDecoder()
    decoded = decoder.feed(body) + decoder.flush()

    # Events with empty data carry no `data:` field content to recover, so the
    # decoder legitimately yields them as empty strings; both sides agree.
    assert [(ev.data, ev.event) for ev in decoded] == [(ev.data, ev.event) for ev in events]


@given(lines=st.lists(st.text(alphabet="abcdefg \t", min_size=1, max_size=20), max_size=8))
def test_sse_parse_never_drops_complete_events(lines: list[str]) -> None:
    """``parse_sse`` round-trips every *complete* event in a self-contained block."""
    raw = "".join(f"data: {line}\n\n" for line in lines)
    events = parse_sse(raw)
    assert [ev.data for ev in events] == lines


@given(
    spans=st.lists(
        st.tuples(st.integers(0, 40), st.integers(0, 40), st.sampled_from(["EMAIL", "PHONE"])),
        max_size=6,
    )
)
def test_merged_spans_never_lose_text(spans: list[tuple[int, int, str]]) -> None:
    """``Detection.merged`` preserves the exact source slice for every span.

    Merging used to extend a span's ``end`` while keeping the shorter span's
    ``text``, silently truncating the value that later gets vaulted.
    """
    source = "".join(string.ascii_lowercase for _ in range(2))[:40]
    detection = Detection(
        spans=[
            Span(start=min(a, b), end=max(a, b), text=source[min(a, b) : max(a, b)], entity_type=t)
            for a, b, t in spans
            if min(a, b) != max(a, b)
        ]
    )

    merged = detection.merged(source)

    for span in merged.spans:
        assert span.text == source[span.start : span.end]
    # Merged spans are disjoint and ordered.
    for prev, nxt in zip(merged.spans, merged.spans[1:], strict=False):
        assert prev.end <= nxt.start
