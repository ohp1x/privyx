"""Tests for SSE parsing and adapters."""

from __future__ import annotations

import pytest

from privyx.streaming import SSEStreamAdapter
from privyx.streaming.sse import SSEDecoder, SSEEvent, parse_sse


def test_parse_sse_single_event() -> None:
    raw = "data: hello world\n\n"
    events = parse_sse(raw)
    assert len(events) == 1
    assert events[0].data == "hello world"


def test_parse_sse_multiple_events() -> None:
    raw = "data: one\n\ndata: two\n\n"
    events = parse_sse(raw)
    assert [e.data for e in events] == ["one", "two"]


def test_sse_roundtrip() -> None:
    event = SSEEvent(data="payload", event="message", id="42")
    parsed = parse_sse(event.to_str())
    assert len(parsed) == 1
    assert parsed[0].data == "payload"
    assert parsed[0].event == "message"
    assert parsed[0].id == "42"


#: Where OpenAI's Chat Completions stream keeps its text.
OPENAI_TEXT = ["choices", "0", "delta", "content"]


def test_adapter_extracts_the_field_it_names() -> None:
    adapter = SSEStreamAdapter(data_path=OPENAI_TEXT)
    event = SSEEvent(
        data='{"choices": [{"delta": {"content": "Hello"}}]}',
        event="chat.completion.chunk",
    )
    assert adapter.extract_delta(event) == "Hello"


def test_sse_decoder_incremental_chunks() -> None:
    from privyx.streaming.sse import SSEDecoder

    decoder = SSEDecoder()
    e1 = decoder.feed("data: hel")
    assert e1 == []
    e2 = decoder.feed("lo\n\n")
    assert len(e2) == 1
    assert e2[0].data == "hello"


def test_utf8_multibyte_split_across_stream_chunks() -> None:
    import json

    from privyx.dialects import OpenAIDialect
    from privyx.streaming import TokenStreamProcessor
    from privyx.token.codec import FormatCodec

    codec = FormatCodec.default()
    router = OpenAIDialect().stream(
        codec, lambda tok: None, lambda: TokenStreamProcessor(codec, lambda tok: None)
    )
    # 4-byte UTF-8 character (emoji 🌟 \U0001F31F: bytes f0 9f 8c 9f)
    text = "Halo 🌟 dunia"
    payload = json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)
    sse_bytes = f"data: {payload}\n\n".encode()

    # Split right in the middle of the 4-byte UTF-8 emoji
    emoji_bytes = "🌟".encode()
    emoji_idx = sse_bytes.find(emoji_bytes)
    assert emoji_idx != -1
    cut = emoji_idx + 2
    part1 = sse_bytes[:cut]
    part2 = sse_bytes[cut:]

    frames1 = router.feed(part1)
    frames2 = router.feed(part2)
    flushed = router.flush()
    all_frames = "".join(frames1 + frames2 + flushed)
    assert "Halo 🌟 dunia" in all_frames


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_sse_decoder_any_line_ending_split_anywhere(newline: str) -> None:
    stream = newline.join(["data: a", "data: b", "", "data: c", "", ""])
    decoder = SSEDecoder()
    events = [event for char in stream for event in decoder.feed(char)]
    events += decoder.flush()
    assert [e.data for e in events] == ["a\nb", "c"]


def test_sse_decoder_emits_a_cr_terminated_event_at_once() -> None:
    decoder = SSEDecoder()
    assert [e.data for e in decoder.feed("data: hello\r\r")] == ["hello"]
    # The LF opening the next chunk paired the last CR: not a blank line.
    assert decoder.feed("\ndata: a\r") == []
    assert [e.data for e in decoder.feed("\n\r\n")] == ["a"]
