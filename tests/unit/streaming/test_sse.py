"""Tests for SSE parsing and adapters."""

from __future__ import annotations

from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
from privyx.streaming.adapters.openai import OpenAIStreamAdapter
from privyx.streaming.sse import SSEEvent, parse_sse


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
