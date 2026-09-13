"""Tests for the streaming deanonymizer — chunk-boundary correctness."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from privyx.streaming.deanonymizer import StreamingDeanonymizer


def _feed_all(segments: list[str], mapping: dict[str, str]) -> str:
    d = StreamingDeanonymizer(mapping)
    parts: list[str] = []
    for seg in segments:
        parts.append(d.feed(seg))
    parts.append(d.flush())
    return "".join(parts)


@given(
    original=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=50),
    pseudo=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ<>_1234567890", min_size=2, max_size=30),
    cut=st.integers(min_value=1, max_value=40),
)
def test_deanonymize_across_chunk_boundaries(original: str, pseudo: str, cut: int) -> None:
    """Pseudonyms split at a single boundary must restore correctly."""
    mapping = {pseudo: original}
    boundary = min(cut, len(pseudo) - 1)
    segments = [pseudo[:boundary], pseudo[boundary:]]
    restored = _feed_all(segments, mapping)
    assert restored == original


@given(
    text=st.text(alphabet="abcdefghijklmnopqrstuvwxyz <>_1234567890", max_size=100),
    splits=st.lists(st.integers(min_value=1, max_value=20), max_size=10),
)
def test_plain_text_passthrough(text: str, splits: list[int]) -> None:
    """Text with no pseudonyms passes through unchanged."""
    segments: list[str] = []
    pos = 0
    for split in splits:
        if pos >= len(text):
            break
        end = min(pos + split, len(text))
        segments.append(text[pos:end])
        pos = end
    if pos < len(text):
        segments.append(text[pos:])
    segments = segments or [text]
    restored = _feed_all(segments, {})
    assert restored == text


def test_multiple_pseudonyms_in_one_chunk() -> None:
    mapping = {"<PRIVYX_EMAIL_1>": "alice@example.com", "<PRIVYX_PHONE_2>": "+1-555-1234"}
    text = "Contact <PRIVYX_EMAIL_1> on <PRIVYX_PHONE_2> today."
    restored = _feed_all([text], mapping)
    assert restored == "Contact alice@example.com on +1-555-1234 today."


def test_pseudonym_split_with_trailing_text() -> None:
    mapping = {"<PRIVYX_EMAIL_1>": "alice@example.com"}
    # Split mid-pseudonym, with trailing text in the second chunk.
    out = _feed_all(["hello <PRIVYX_EMAI", "L_1>!"], mapping)
    assert out == "hello alice@example.com!"


def test_update_mapping_mid_stream() -> None:
    d = StreamingDeanonymizer()
    d.feed("x")
    d.update_mapping({"<PRIVYX_EMAIL_1>": "bob@example.com"})
    out = d.feed("<PRIVYX_EMAIL_1>") + d.flush()
    assert out == "bob@example.com"