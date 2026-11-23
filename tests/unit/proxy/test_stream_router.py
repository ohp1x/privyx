"""Unit tests for :class:`StreamRouter` — text, tool-call buffering, ordering."""

from __future__ import annotations

import json
from typing import Any

import pytest

from privyx.dialects import AnthropicDialect, Dialect, OpenAIDialect, ResponsesDialect
from privyx.operators import FPEOperator, PseudonymOperator
from privyx.streaming import SSEStreamAdapter, StreamRouter, select_processor_factory
from privyx.streaming.deanonymizer import StreamingDeanonymizer, TokenStreamProcessor
from privyx.streaming.sse import SSEDecoder, SSEEvent
from privyx.token.codec import FormatCodec

TOKEN = "<PRIVYX_EMAIL_1>"
ORIG = "alice@example.com"


class _Format:
    """A dialect, and where the text of its stream is, to read it back."""

    def __init__(self, dialect: Dialect, extract: Any) -> None:
        self.dialect = dialect
        self.extract_delta = extract


def _anthropic_text(event: SSEEvent) -> str:
    try:
        payload = json.loads(event.data)
    except json.JSONDecodeError:
        return ""
    delta = payload.get("delta", {}) if payload.get("type") == "content_block_delta" else {}
    text = delta.get("text", "") if delta.get("type") == "text_delta" else ""
    return text if isinstance(text, str) else ""


def OpenAIStreamAdapter() -> _Format:  # noqa: N802 - stands in for a class
    text = SSEStreamAdapter(data_path=["choices", "0", "delta", "content"])
    return _Format(OpenAIDialect(), text.extract_delta)


def AnthropicStreamAdapter() -> _Format:  # noqa: N802 - stands in for a class
    return _Format(AnthropicDialect(), _anthropic_text)


def _responses() -> _Format:
    return _Format(ResponsesDialect(), SSEStreamAdapter().extract_delta)


def _stream(fmt: _Format, codec: FormatCodec, resolve: Any, make: Any = None) -> StreamRouter:
    make = make or (lambda: TokenStreamProcessor(codec, resolve))
    return fmt.dialect.stream(codec, resolve, make)


def _router(fmt: _Format, mapping: dict[str, str]) -> StreamRouter:
    return _stream(fmt, FormatCodec.default(), mapping.get)


def _run(router: StreamRouter, body: str) -> str:
    return "".join(router.feed(body) + router.flush())


def _events(raw: str) -> list[Any]:
    decoder = SSEDecoder()
    return [
        json.loads(ev.data) for ev in decoder.feed(raw) + decoder.flush() if ev.data != "[DONE]"
    ]


def _collect_text(raw: str, adapter: _Format) -> str:
    decoder = SSEDecoder()
    return "".join(adapter.extract_delta(ev) for ev in decoder.feed(raw) + decoder.flush())


def _openai_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# --------------------------------------------------------------------------
# OpenAI


def test_openai_text_deanon_and_done_is_last() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    body = (
        "".join(
            _openai_frame({"choices": [{"delta": {"content": c}}]})
            for c in ["Hi ", TOKEN[:8], TOKEN[8:], " bye"]
        )
        + "data: [DONE]\n\n"
    )

    out = _run(router, body)

    assert TOKEN not in out
    assert _collect_text(out, adapter) == f"Hi {ORIG} bye"
    assert out.endswith("data: [DONE]\n\n")


def test_openai_held_tail_flushed_before_done() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {})  # empty mapping: token-shaped text stays verbatim
    body = (
        _openai_frame({"choices": [{"delta": {"content": "end <PRIVYX_EMA"}}]}) + "data: [DONE]\n\n"
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
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "send", "arguments": ""},
                                }
                            ]
                        },
                    }
                ]
            }
        )
    ]
    frames += [
        _openai_frame(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": fragment}}]
                        },
                    }
                ]
            }
        )
        for fragment in fragments
    ]
    frames.append(
        _openai_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    )
    frames.append("data: [DONE]\n\n")

    out = _run(router, "".join(frames))

    assert TOKEN not in out
    tool_frames = [ev for ev in _events(out) if ev["choices"][0]["delta"].get("tool_calls")]
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
        _openai_frame(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c",
                                    "function": {"name": "f", "arguments": '{"to": "al'},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        _openai_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
        "data: [DONE]\n\n",
    ]

    out = _run(router, "".join(frames))

    # Malformed args never form a JSON tool call, so none is forwarded.
    assert "tool_calls" not in out


def test_openai_refusal_and_reasoning_split_tokens_restored() -> None:
    adapter = OpenAIStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    body = (
        "".join(
            _openai_frame({"choices": [{"index": 0, "delta": {field: part}}]})
            for field in ("reasoning", "refusal")
            for part in ("user ", TOKEN[:7], TOKEN[7:], ".")
        )
        + _openai_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        + "data: [DONE]\n\n"
    )

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


def test_legacy_completions_finish_frame_forwarded() -> None:
    router = _router(OpenAIStreamAdapter(), {TOKEN: ORIG})
    frames = [
        _openai_frame(
            {
                "id": "cmpl-1",
                "object": "text_completion",
                "choices": [{"text": "Hello ", "index": 0}],
            }
        ),
        _openai_frame(
            {
                "id": "cmpl-1",
                "object": "text_completion",
                "choices": [{"text": TOKEN, "index": 0}],
            }
        ),
        _openai_frame(
            {
                "id": "cmpl-1",
                "object": "text_completion",
                "choices": [{"text": "", "index": 0, "finish_reason": "stop"}],
            }
        ),
        "data: [DONE]\n\n",
    ]
    out = _run(router, "".join(frames))
    events = _events(out)
    finish_events = [ev for ev in events if ev["choices"][0].get("finish_reason") == "stop"]
    assert len(finish_events) == 1
    assert finish_events[0]["choices"][0]["finish_reason"] == "stop"
    assert "delta" not in finish_events[0]["choices"][0]
    assert ORIG in out


def test_legacy_completions_flush_tail_uses_choices_text() -> None:
    router = _router(OpenAIStreamAdapter(), {TOKEN: ORIG})
    part1 = TOKEN[:8]
    part2 = TOKEN[8:]
    frame1 = _openai_frame(
        {
            "id": "cmpl-1",
            "object": "text_completion",
            "choices": [{"text": f"Contact {part1}", "index": 0}],
        }
    )
    frame2 = _openai_frame(
        {
            "id": "cmpl-1",
            "object": "text_completion",
            "choices": [{"text": part2, "index": 0}],
        }
    )
    out1 = "".join(router.feed(frame1))
    out2 = "".join(router.feed(frame2))
    flushed = "".join(router.flush())
    combined = out1 + out2 + flushed
    events = _events(combined)
    assert ORIG in combined
    for ev in events:
        assert "text" in ev["choices"][0]
        assert "delta" not in ev["choices"][0]


def test_legacy_completions_multiple_choices_isolated() -> None:
    router = _router(OpenAIStreamAdapter(), {TOKEN: ORIG})
    part1 = TOKEN[:8]
    part2 = TOKEN[8:]
    frame1 = _openai_frame(
        {
            "id": "cmpl-multi",
            "object": "text_completion",
            "choices": [
                {"text": f"Choice0: {part1}", "index": 0},
                {"text": "Choice1: regular text", "index": 1},
            ],
        }
    )
    frame2 = _openai_frame(
        {
            "id": "cmpl-multi",
            "object": "text_completion",
            "choices": [
                {"text": f"{part2} end0", "index": 0, "finish_reason": "stop"},
                {"text": " more1", "index": 1, "finish_reason": "stop"},
            ],
        }
    )
    out1 = "".join(router.feed(frame1))
    out2 = "".join(router.feed(frame2))
    flushed = "".join(router.flush())
    combined = out1 + out2 + flushed
    events = _events(combined)

    choice1_texts = [
        c["text"] for ev in events for c in ev.get("choices", []) if c.get("index") == 1
    ]
    full_choice1 = "".join(choice1_texts)
    assert ORIG not in full_choice1
    assert "regular text more1" in full_choice1

    choice0_texts = [
        c["text"] for ev in events for c in ev.get("choices", []) if c.get("index") == 0
    ]
    full_choice0 = "".join(choice0_texts)
    assert ORIG in full_choice0
    assert TOKEN not in full_choice0


def test_legacy_completions_flush_does_not_repeat_other_choices() -> None:
    router = _router(OpenAIStreamAdapter(), {TOKEN: ORIG})
    part1 = TOKEN[:8]
    frame = _openai_frame(
        {
            "id": "cmpl-multi",
            "object": "text_completion",
            "choices": [
                {"text": f"C0: {part1}", "index": 0},
                {"text": "C1: unique_text_done", "index": 1},
            ],
        }
    )
    router.feed(frame)
    flushed_frames = router.flush()
    flushed_events = [json.loads(ev.data) for ev in SSEDecoder().feed("".join(flushed_frames))]
    assert len(flushed_events) == 1
    # Flushed frame must only contain Choice 0
    assert len(flushed_events[0]["choices"]) == 1
    assert flushed_events[0]["choices"][0]["index"] == 0
    assert "unique_text_done" not in flushed_events[0]["choices"][0]["text"]


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
    fmt = AnthropicStreamAdapter()
    router = _router(fmt, {TOKEN: ORIG})

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
    assert fmt.dialect._thinking["sig-stream-1"] == f"mail {TOKEN} or ops@vendor.test"


@pytest.mark.parametrize("written", [TOKEN, TOKEN.strip("<>")], ids=["full", "bare"])
def test_anthropic_tool_use_buffered_and_restored(written: str) -> None:
    adapter = AnthropicStreamAdapter()
    router = _router(adapter, {TOKEN: ORIG})
    full = json.dumps({"to": written})
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

    assert written not in out
    input_events = [
        ev for ev in _events(out) if ev.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert len(input_events) == 1
    assert json.loads(input_events[0]["delta"]["partial_json"])["to"] == ORIG


def test_anthropic_restores_a_token_id_written_on_its_own() -> None:
    """In a tool's input and in text, where the id comes split across deltas."""
    token, ident, name = "<PRIVYX_PERSON_9F3A1C2B7D4E5F60>", "9F3A1C2B7D4E5F60", "Alice Example"
    mapping = {token: name}
    codec, adapter = FormatCodec.default(), AnthropicStreamAdapter()
    router = _stream(
        adapter,
        codec,
        mapping.get,
        select_processor_factory(PseudonymOperator(), codec, mapping.get, mapping),
    )

    def delta(index: int, payload: dict[str, Any]) -> str:
        body = {"type": "content_block_delta", "index": index, "delta": payload}
        return _anthropic("content_block_delta", body)

    def block(index: int, kind: str, *deltas: str) -> str:
        start = {"type": "content_block_start", "index": index, "content_block": {"type": kind}}
        stop = {"type": "content_block_stop", "index": index}
        return (
            _anthropic("content_block_start", start)
            + "".join(deltas)
            + _anthropic("content_block_stop", stop)
        )

    arguments = json.dumps({"query": ident})
    out = _run(
        router,
        block(
            0,
            "tool_use",
            delta(0, {"type": "input_json_delta", "partial_json": arguments[:14]}),
            delta(0, {"type": "input_json_delta", "partial_json": arguments[14:]}),
        )
        + block(
            1,
            "text",
            delta(1, {"type": "text_delta", "text": f"Found {ident[:7]}"}),
            delta(1, {"type": "text_delta", "text": f"{ident[7:]}."}),
        ),
    )

    assert ident not in out
    deltas = [ev["delta"] for ev in _events(out) if ev["type"] == "content_block_delta"]
    assert [json.loads(d["partial_json"]) for d in deltas if "partial_json" in d] == [
        {"query": name}
    ]
    assert "".join(d.get("text", "") for d in deltas) == f"Found {name}."


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
    router = _router(_responses(), {TOKEN: ORIG})
    ids = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    text = f"Hi {TOKEN}!"
    part = {"type": "output_text", "text": text, "annotations": []}
    message = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [part],
    }
    body = "".join(
        [
            _rs(
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": {"id": "resp_1", "output": []},
                }
            ),
            _rs(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "delta": "Hi " + TOKEN[:6],
                    **ids,
                }
            ),
            _rs(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 2,
                    "delta": TOKEN[6:] + "!",
                    **ids,
                }
            ),
            _rs({"type": "response.output_text.done", "sequence_number": 3, "text": text, **ids}),
            _rs({"type": "response.content_part.done", "sequence_number": 4, "part": part, **ids}),
            _rs(
                {
                    "type": "response.output_item.done",
                    "sequence_number": 5,
                    "output_index": 0,
                    "item": message,
                }
            ),
            _rs(
                {
                    "type": "response.completed",
                    "sequence_number": 6,
                    "response": {"id": "resp_1", "output": [message]},
                }
            ),
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
    router = _router(_responses(), {TOKEN: ORIG})
    ids = {"item_id": "fc_1", "output_index": 1}
    args = json.dumps({"to": TOKEN})
    body = "".join(
        [
            *[
                _rs({"type": "response.function_call_arguments.delta", "delta": part, **ids})
                for part in (args[:5], args[5:14], args[14:])
            ],
            _rs(
                {
                    "type": "response.function_call_arguments.done",
                    "arguments": args,
                    "name": "send",
                    **ids,
                }
            ),
            _rs(
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "send",
                        "arguments": args,
                    },
                }
            ),
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
    router = _stream(
        adapter, FormatCodec.default(), mapping.get, lambda: StreamingDeanonymizer(mapping)
    )
    body = (
        "".join(
            _openai_frame({"choices": [{"delta": {"content": c}}]})
            for c in ["Hello ", "Jane ", "Fake", "!"]
        )
        + "data: [DONE]\n\n"
    )

    out = _run(router, body)

    assert "Jane Fake" not in out
    assert _collect_text(out, adapter) == "Hello Alice Real!"
    assert out.endswith("data: [DONE]\n\n")


def test_select_processor_factory_picks_recognizer_by_operator() -> None:
    mapping = {"fake": "real"}
    codec = FormatCodec.default()

    literal = select_processor_factory(FPEOperator(), codec, mapping.get, mapping)
    token = select_processor_factory(PseudonymOperator(), codec, mapping.get, mapping)

    assert isinstance(literal(), StreamingDeanonymizer)
    assert isinstance(token(), TokenStreamProcessor)


def test_a_held_back_chat_tail_keeps_the_rest_of_its_choice() -> None:
    router = _router(OpenAIStreamAdapter(), {TOKEN: ORIG})
    choice = {
        "index": 1,
        "delta": {"content": "hi " + TOKEN[:6]},
        "logprobs": None,
        "finish_reason": None,
    }

    events = _events(_run(router, _openai_frame({"id": "c1", "choices": [choice]})))

    assert events[-1]["choices"] == [{**choice, "delta": {"content": TOKEN[:6]}}]


def _chat(text: str) -> str:
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n"


@pytest.mark.parametrize(
    ("fmt", "frame"),
    [
        (OpenAIStreamAdapter, _chat),
        (
            AnthropicStreamAdapter,
            lambda text: (
                "event: content_block_delta\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    }
                )
                + "\n\n"
            ),
        ),
    ],
    ids=["openai", "anthropic"],
)
def test_a_comment_inside_a_token_goes_out_and_the_token_stays_whole(fmt: Any, frame: Any) -> None:
    adapter = fmt()
    body = frame(TOKEN[:6]) + ": keep-alive\n\n" + frame(TOKEN[6:])

    out = _run(_router(adapter, {TOKEN: ORIG}), body)

    assert ": keep-alive\n\n" in out
    assert _collect_text(out, adapter) == ORIG


def test_a_plain_text_event_keeps_its_id_and_a_comment_stays() -> None:
    codec = FormatCodec.default()
    router = StreamRouter(codec, {TOKEN: ORIG}.get)

    out = _run(router, f": hello\n\nid: 7\nevent: tick\ndata: to {TOKEN}\n\n")

    assert out == f": hello\n\nevent: tick\nid: 7\ndata: to {ORIG}\n\n"


def _client_state(raw: str) -> tuple[str, int | None, int]:
    """The last event id, the retry, and the events a client following the SSE rules has."""
    buffer, last, retry, data, events = "", "", None, False, 0
    for line in raw.split("\n"):
        if not line:
            last = buffer
            events, data = events + data, False
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data = True
            elif field == "id":
                buffer = value
            elif field == "retry" and value.isdigit():
                retry = int(value)
    return last, retry, events


_TEXT_FRAMES = {
    "openai": (
        OpenAIStreamAdapter,
        lambda t: "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": t}}]}),
    ),
    "anthropic": (
        AnthropicStreamAdapter,
        lambda t: (
            "event: content_block_delta\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": t},
                }
            )
        ),
    ),
    "responses": (
        _responses,
        lambda t: (
            "event: response.output_text.delta\ndata: "
            + json.dumps(
                {
                    "type": "response.output_text.delta",
                    "item_id": "i",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": t,
                }
            )
        ),
    ),
}


@pytest.mark.parametrize("name", list(_TEXT_FRAMES))
@pytest.mark.parametrize(
    "shape",
    [
        "{a}\n\nid: 41\n\n{b}\n\n",
        "retry: 5000\n\n{a}\n\n{b}\n\n",
        "id: 42\n{a}\n\nid:\n{b}\n\n",
        "id: 42\n{a}\n\nid: 43\n\n{b}\n\n",
    ],
)
def test_the_client_ends_with_the_id_and_retry_the_upstream_sent(name: str, shape: str) -> None:
    fmt, frame = _TEXT_FRAMES[name]
    body = shape.format(a=frame("one "), b=frame("two"))

    out = _run(_router(fmt(), {}), body)

    assert _client_state(out) == _client_state(body)


@pytest.mark.parametrize("name", list(_TEXT_FRAMES))
def test_a_control_block_inside_a_token_leaves_the_token_whole(name: str) -> None:
    fmt, frame = _TEXT_FRAMES[name]
    body = f"{frame(TOKEN[:6])}\n\nid: 9\n\nretry: 70\n\n{frame(TOKEN[6:])}\n\n"

    out = _run(_router(fmt(), {TOKEN: ORIG}), body)

    assert ORIG in out  # restored in one piece
    assert _client_state(out) == ("9", 70, 1)


@pytest.mark.parametrize("name", list(_TEXT_FRAMES))
def test_the_id_of_an_event_held_back_still_reaches_the_client(name: str) -> None:
    fmt, frame = _TEXT_FRAMES[name]
    body = f"id: 5\n{frame(TOKEN[:6])}\n\n{frame(TOKEN[6:])}\n\n"

    out = _run(_router(fmt(), {TOKEN: ORIG}), body)

    assert ORIG in out
    assert _client_state(out)[0] == "5"


def test_a_held_tail_does_not_bring_back_an_older_id() -> None:
    fmt, frame = _TEXT_FRAMES["openai"]

    out = _run(_router(fmt(), {}), f"id: 1\n{frame('end <PRIVYX_EMA')}\n\nid: 2\n\n")

    assert _client_state(out)[0] == "2"


@pytest.mark.parametrize(
    ("fmt", "terminal"),
    [
        (OpenAIStreamAdapter, "data: [DONE]"),
        (AnthropicStreamAdapter, 'event: message_stop\ndata: {"type":"message_stop"}'),
    ],
    ids=["openai", "anthropic"],
)
def test_a_terminal_event_goes_last_without_bringing_back_its_id(fmt: Any, terminal: str) -> None:
    out = _run(_router(fmt(), {}), f"id: 9\nretry: 70\n{terminal}\n\nid: 10\n\n")

    assert _client_state(out) == ("10", 70, 1)
    assert out.endswith(f"{terminal}\n\n")
