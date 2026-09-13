"""Property test: chunk-boundary pseudonym restoration.

Any contiguous substring of a pseudonym that is fed across boundaries must
restore to the original value exactly once.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from privyx.streaming.deanonymizer import StreamingDeanonymizer


@given(
    pseudo=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ<>_1234567890", min_size=2, max_size=50),
    original=st.text(alphabet="abcdefghijklmnopqrstuvwxyz @.", min_size=1, max_size=40),
    boundaries=st.lists(
        st.integers(min_value=0, max_value=2), max_size=10
    ),
)
def test_chunk_boundaries(pseudo: str, original: str, boundaries: list[int]) -> None:
    """Splitting a pseudonym at any boundaries restores to the original."""
    mapping = {pseudo: original}

    # Build a chunking that splits the pseudonym into (possibly many) pieces.
    pieces: list[str] = []
    i = 0
    for step in boundaries:
        if i >= len(pseudo):
            break
        take = min(max(step, 1), len(pseudo) - i)
        pieces.append(pseudo[i : i + take])
        i += take
    if i < len(pseudo):
        pieces.append(pseudo[i:])
    pieces = pieces or [pseudo]

    d = StreamingDeanonymizer(mapping)
    out = "".join([d.feed(p) for p in pieces] + [d.flush()])
    assert out == original