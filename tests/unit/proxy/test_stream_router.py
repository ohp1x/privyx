"""Unit tests for :class:`StreamRouter` — text, tool-call buffering, ordering."""

from __future__ import annotations

import json
from typing import Any

from privyx.proxy.stream_router import StreamRouter, select_processor_factory
from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.adapters.openai import OpenAIStreamAdapter
from privyx.streaming.deanonymizer import StreamingDeanonymizer, TokenStreamProcessor
from privyx.streaming.sse import SSEDecoder
from privyx.token.codec import FormatCodec

TOKEN = "<PRIVYX_EMAIL_1>"
ORIG = "alice@example.com"


def _router(adapter: SSEStreamAdapter, mapping: dict[str, str]) -> StreamRouter:
    return StreamRouter(FormatCodec.default(), mapping.get, adapter)


def _run(router: StreamRouter, body: str) -> str:
    return "".join(router.feed(body) + router.flush())


def _events(raw: str) -> list[Any]:
    decoder = SSEDecoder()
    return [
        json.loads(ev.data)
        for ev in decoder.feed(raw) + decoder.flush()
        if ev.data != "[DONE]"
    ]


def _collect_text(raw: str, adapter: SSEStreamAdapter) -> str:
    decoder = SSEDecoder()
    return "".join(adapter.extract_delta(ev) for ev in decoder.feed(raw) + decoder.flush())


def _openai_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# --------------------------------------------------------------------------
# OpenAI


def test_openai_text_deanon_and_done_is_last() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    body = "".join(
        _openai_frame({"choices": [{"delta": {"content": c}}]})
        for c in ["Hi ", TOKEN[:8], TOKEN[8:], " bye"]
    ) + "data: [DONE]\n\n"

    out = _run(router, body)

    assert TOKEN not in out
    assert _collect_text(out, adapter) == f"Hi {ORIG} bye"
    assert out.endswith("data: [DONE]\n\n")


def test_openai_held_tail_flushed_before_done() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {})  # empty mapping: token-shaped text stays verbatim
    body = (
        _openai_frame({"choices": [{"delta": {"content": "end <PRIVYX_EMA"}}]})
        + "data: [DONE]\n\n"
    )

    out = _run(router, body)

    assert _collect_text(out, adapter) == "end <PRIVYX_EMA"
    assert out.endswith("data: [DONE]\n\n")


def test_openai_tool_calls_buffered_and_restored_before_done() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    args = json.dumps({"to": TOKEN})
    fragments = [args[:5], args[5:12], args[12:]]

    frames = [
        _openai_frame(
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "send", "arguments": ""}}
            ]}}]}
        )
    ]
    frames += [
        _openai_frame({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": fragment}}
        ]}}]})
        for fragment in fragments
    ]
    frames.append(
        _openai_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    )
    frames.append("data: [DONE]\n\n")

    out = _run(router, "".join(frames))

    assert TOKEN not in out
    tool_frames = [
        ev for ev in _events(out) if ev["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_frames) == 1
    call = tool_frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "send"
    assert json.loads(call["function"]["arguments"])["to"] == ORIG
    assert out.endswith("data: [DONE]\n\n")


def test_openai_truncated_tool_call_is_dropped() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {})
    frames = [
        _openai_frame({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "f", "arguments": '{"to": "al'}}
        ]}}]}),
        _openai_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
        "data: [DONE]\n\n",
    ]

    out = _run(router, "".join(frames))

    # Malformed args never form a JSON tool call, so none is forwarded.
    assert "tool_calls" not in out


# --------------------------------------------------------------------------
# Anthropic


def _anthropic(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def test_anthropic_text_deanon_and_message_stop_last() -> None:
    adapter = AnthropicStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})

    def text_delta(text: str) -> str:
        return _anthropic(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    body = "".join(
        [
            _anthropic("message_start", {"type": "message_start"}),
            _anthropic(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            ),
            text_delta("Hi "),
            text_delta(TOKEN[:9]),
            text_delta(TOKEN[9:]),
            text_delta("!"),
            _anthropic("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _anthropic("message_stop", {"type": "message_stop"}),
        ]
    )

    out = _run(router, body)

    assert TOKEN not in out
    assert _collect_text(out, adapter) == f"Hi {ORIG}!"
    assert _events(out)[-1] == {"type": "message_stop"}


def test_anthropic_tool_use_buffered_and_restored() -> None:
    adapter = AnthropicStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    full = json.dumps({"to": TOKEN})
    fragments = [full[:4], full[4:10], full[10:]]

    def input_json(partial: str) -> str:
        return _anthropic(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            },
        )

    body = "".join(
        [
            _anthropic(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "name": "send"},
                },
            ),
            *[input_json(fragment) for fragment in fragments],
            _anthropic("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _anthropic("message_stop", {"type": "message_stop"}),
        ]
    )

    out = _run(router, body)

    assert TOKEN not in out
    input_events = [
        ev for ev in _events(out) if ev.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert len(input_events) == 1
    assert json.loads(input_events[0]["delta"]["partial_json"])["to"] == ORIG


# --------------------------------------------------------------------------
# Literal (faker) restore: match exact substituted values, not codec tokens


def test_literal_factory_restores_substituted_values_across_chunks() -> None:
    """A trie factory restores plain fake values even when split across deltas."""
    adapter = OpenAIStreamAdapter()
    mapping = {"Jane Fake": "Alice Real"}
    router = StreamRouter(
        FormatCodec.default(),
        mapping.get,
        adapter,
        make_processor=lambda: StreamingDeanonymizer(mapping),
    )
    body = "".join(
        _openai_frame({"choices": [{"delta": {"content": c}}]})
        for c in ["Hello ", "Jane ", "Fake", "!"]
    ) + "data: [DONE]\n\n"

    out = _run(router, body)

    assert "Jane Fake" not in out
    assert _collect_text(out, adapter) == "Hello Alice Real!"
    assert out.endswith("data: [DONE]\n\n")


def test_select_processor_factory_picks_recognizer_by_operator() -> None:
    class _Literal:
        stream_restore = "literal"

    class _Token:
        pass  # no attribute → defaults to the codec recognizer

    mapping = {"fake": "real"}
    codec = FormatCodec.default()

    literal = select_processor_factory(_Literal(), codec, mapping.get, mapping)
    token = select_processor_factory(_Token(), codec, mapping.get, mapping)

    assert isinstance(literal(), StreamingDeanonymizer)
    assert isinstance(token(), TokenStreamProcessor)
