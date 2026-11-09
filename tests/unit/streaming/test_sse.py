"""Tests for SSE parsing and adapters."""

from __future__ import annotations

import pytest

from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
from privyx.streaming.adapters.openai import OpenAIStreamAdapter
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


def test_openai_adapter_extracts_content() -> None:
    adapter = OpenAIStreamAdapter()
    event = SSEEvent(
        data='{"choices": [{"delta": {"content": "Hello"}}]}',
        event="chat.completion.chunk",
    )
    assert adapter.extract_delta(event) == "Hello"


def test_openai_adapter_roundtrip() -> None:
    adapter = OpenAIStreamAdapter()
    original = SSEEvent(
        data='{"choices": [{"delta": {"content": "x"}}]}',
        event="chat.completion.chunk",
    )
    wrapped = adapter.wrap_delta("Hello", original)
    parsed = parse_sse(wrapped.to_str())[0]
    assert adapter.extract_delta(parsed) == "Hello"


def test_anthropic_adapter_ignores_thinking_delta() -> None:
    adapter = AnthropicStreamAdapter()
    payload = (
        '{"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "..."}}'
    )
    event = SSEEvent(data=payload)
    assert adapter.extract_delta(event) == ""


def test_anthropic_adapter_extracts_text_delta() -> None:
    adapter = AnthropicStreamAdapter()
    event = SSEEvent(
        data='{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}'
    )
    assert adapter.extract_delta(event) == "Hi"


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

    from privyx.proxy.stream_router import StreamRouter
    from privyx.streaming.adapters.openai import OpenAIStreamAdapter
    from privyx.token.codec import FormatCodec

    router = StreamRouter(FormatCodec.default(), lambda tok: None, OpenAIStreamAdapter())
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
