"""Unit tests for :class:`StreamRouter` — text, tool-call buffering, ordering."""

from __future__ import annotations

import json
from typing import Any

from privyx.proxy import schemas
from privyx.proxy.stream_router import StreamRouter, select_processor_factory
from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.adapters.openai import OpenAIStreamAdapter
from privyx.streaming.adapters.registry import build_stream_adapter
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


def test_openai_refusal_and_reasoning_split_tokens_restored() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    body = "".join(
        _openai_frame({"choices": [{"index": 0, "delta": {field: part}}]})
        for field in ("reasoning", "refusal")
        for part in ("user ", TOKEN[:7], TOKEN[7:], ".")
    ) + _openai_frame(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    ) + "data: [DONE]\n\n"

    out = _run(router, body)

    assert TOKEN not in out
    deltas = [ev["choices"][0]["delta"] for ev in _events(out)]
    for field in ("reasoning", "refusal"):
        assert "".join(d.get(field, "") for d in deltas) == f"user {ORIG}."
    assert out.endswith("data: [DONE]\n\n")


def test_openai_legacy_function_call_buffered_and_restored() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    args = json.dumps({"to": TOKEN})
    frames = [
        _openai_frame({"choices": [{"index": 0, "delta": {"function_call": {"name": "send"}}}]}),
        *[
            _openai_frame(
                {"choices": [{"index": 0, "delta": {"function_call": {"arguments": part}}}]}
            )
            for part in (args[:9], args[9:])
        ],
        _openai_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "function_call"}]}),
        "data: [DONE]\n\n",
    ]

    out = _run(router, "".join(frames))

    assert TOKEN not in out
    calls = [
        ev["choices"][0]["delta"]["function_call"]
        for ev in _events(out)
        if "function_call" in ev["choices"][0]["delta"]
    ]
    assert len(calls) == 1 and calls[0]["name"] == "send"
    assert json.loads(calls[0]["arguments"])["to"] == ORIG


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


def test_anthropic_thinking_remembered_as_sent_by_signature() -> None:
    router = _router(AnthropicStreamAdapter(), {TOKEN: ORIG})

    def delta(payload: dict[str, Any]) -> str:
        return _anthropic(
            "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": payload}
        )

    start = {"type": "thinking", "thinking": "", "signature": ""}
    out = _run(
        router,
        _anthropic(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": start},
        )
        + delta({"type": "thinking_delta", "thinking": f"mail {TOKEN[:9]}"})
        + delta({"type": "thinking_delta", "thinking": f"{TOKEN[9:]} or ops@vendor.test"})
        + delta({"type": "signature_delta", "signature": "sig-stream-1"})
        + _anthropic("content_block_stop", {"type": "content_block_stop", "index": 0}),
    )

    assert TOKEN not in out  # the client sees it restored...
    # ...while the upstream's own text is kept for when the client echoes it back.
    assert schemas._THINKING["sig-stream-1"] == f"mail {TOKEN} or ops@vendor.test"


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


def test_anthropic_citations_delta_restored_whole() -> None:
    adapter = AnthropicStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    body = _anthropic(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "citations_delta",
                "citation": {"type": "char_location", "cited_text": f"mail {TOKEN}"},
            },
        },
    )

    out = _run(router, body)

    assert out.startswith("event: content_block_delta\n")
    assert _events(out)[0]["delta"]["citation"]["cited_text"] == f"mail {ORIG}"


# --------------------------------------------------------------------------
# OpenAI Responses


def _rs(payload: dict[str, Any]) -> str:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"


def test_responses_split_text_done_and_completed_restored() -> None:
    router = _router(build_stream_adapter("responses"), {TOKEN: ORIG})
    ids = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    text = f"Hi {TOKEN}!"
    part = {"type": "output_text", "text": text, "annotations": []}
    message = {
        "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
        "content": [part],
    }
    body = "".join(
        [
            _rs({"type": "response.created", "sequence_number": 0,
                 "response": {"id": "resp_1", "output": []}}),
            _rs({"type": "response.output_text.delta", "sequence_number": 1,
                 "delta": "Hi " + TOKEN[:6], **ids}),
            _rs({"type": "response.output_text.delta", "sequence_number": 2,
                 "delta": TOKEN[6:] + "!", **ids}),
            _rs({"type": "response.output_text.done", "sequence_number": 3,
                 "text": text, **ids}),
            _rs({"type": "response.content_part.done", "sequence_number": 4,
                 "part": part, **ids}),
            _rs({"type": "response.output_item.done", "sequence_number": 5,
                 "output_index": 0, "item": message}),
            _rs({"type": "response.completed", "sequence_number": 6,
                 "response": {"id": "resp_1", "output": [message]}}),
        ]
    )

    out = _run(router, body)

    assert TOKEN not in out
    events = _events(out)
    types = [ev["type"] for ev in events]
    assert types[-1] == "response.completed"
    assert "event: response.output_text.delta\n" in out  # event lines preserved
    deltas = "".join(ev["delta"] for ev in events if ev["type"] == "response.output_text.delta")
    assert deltas == f"Hi {ORIG}!"
    done = next(ev for ev in events if ev["type"] == "response.output_text.done")
    assert done["text"] == f"Hi {ORIG}!"
    item = next(ev for ev in events if ev["type"] == "response.output_item.done")["item"]
    assert item["content"][0]["text"] == f"Hi {ORIG}!" and item["id"] == "msg_1"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == f"Hi {ORIG}!"


def test_responses_function_call_arguments_buffered_until_done() -> None:
    router = _router(build_stream_adapter("responses"), {TOKEN: ORIG})
    ids = {"item_id": "fc_1", "output_index": 1}
    args = json.dumps({"to": TOKEN})
    body = "".join(
        [
            *[
                _rs({"type": "response.function_call_arguments.delta", "delta": part, **ids})
                for part in (args[:5], args[5:14], args[14:])
            ],
            _rs({"type": "response.function_call_arguments.done", "arguments": args,
                 "name": "send", **ids}),
            _rs({"type": "response.output_item.done", "output_index": 1, "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "send", "arguments": args}}),
        ]
    )

    out = _run(router, body)

    assert TOKEN not in out
    events = _events(out)
    deltas = [ev for ev in events if ev["type"] == "response.function_call_arguments.delta"]
    assert len(deltas) == 1 and json.loads(deltas[0]["delta"])["to"] == ORIG
    assert events.index(deltas[0]) < [ev["type"] for ev in events].index(
        "response.function_call_arguments.done"
    )
    assert json.loads(events[-1]["item"]["arguments"])["to"] == ORIG


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
