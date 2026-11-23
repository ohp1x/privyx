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


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_parse_sse_reads_any_line_ending_and_drops_an_unfinished_event(newline: str) -> None:
    text = newline.join(["data: a", "", "data: b", "", "data: cut"])

    assert [e.data for e in parse_sse(text)] == ["a", "b"]


def test_parse_sse_returns_only_events_with_data() -> None:
    assert parse_sse(": keep-alive\n\nid: 7\n\n") == []


def test_sse_decoder_gives_comments_only_when_asked() -> None:
    text = ": one\n\n: two\ndata: a\n\n: three"

    plain = SSEDecoder()
    assert [e.data for e in plain.feed(text) + plain.flush()] == ["a"]
    kept = SSEDecoder(comments=True)
    assert [e.to_str() for e in kept.feed(text) + kept.flush()] == [
        ": one\n\n",
        ": two\n\n",
        "data: a\n\n",
        ": three\n\n",
    ]


def test_sse_decoder_keeps_a_block_of_only_id_or_retry_when_asked() -> None:
    text = "id: 41\n\nretry: 5000\n\nid:\nevent: ignored\n\ndata: a\n\n"

    assert [e.data for e in SSEDecoder().feed(text)] == ["a"]
    assert [e.to_str() for e in SSEDecoder(comments=True).feed(text)] == [
        "id: 41\n\n",
        "retry: 5000\n\n",
        "id:\n\n",
        "data: a\n\n",
    ]


def test_an_empty_id_is_written_so_it_resets_the_clients() -> None:
    assert parse_sse("id:\ndata: a\n\n")[0].to_str() == "id: \ndata: a\n\n"


@pytest.mark.parametrize("value", ["+5", "-5", "5_0", "５", "x"])
def test_a_retry_a_client_would_ignore_is_not_rewritten(value: str) -> None:
    assert parse_sse(f"retry: {value}\ndata: a\n\n")[0].retry is None


@pytest.mark.parametrize("invalid", ["x", "+5", "9" * 5000])
def test_an_invalid_retry_leaves_the_last_valid_one(invalid: str) -> None:
    assert parse_sse(f"retry: 5000\nretry: {invalid}\ndata: a\n\n")[0].retry == 5000


def test_an_id_with_a_nul_leaves_the_last_valid_one() -> None:
    assert parse_sse("id: 7\nid: a\0b\ndata: a\n\n")[0].id == "7"


def test_a_very_long_retry_does_not_stop_the_stream() -> None:
    text = f"retry: {'9' * 5000}\n\ndata: a\n\n"

    assert [e.data for e in SSEDecoder().feed(text)] == ["a"]
    assert SSEDecoder(comments=True).feed(text)[-1].data == "a"
