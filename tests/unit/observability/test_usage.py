"""Token usage: the three provider shapes and cache hit rate."""

from __future__ import annotations

import pytest

from privyx.observability.usage import UsageMeter, cache_hit_rate


def test_anthropic_stream_usage_keeps_the_last_running_count() -> None:
    meter = UsageMeter()
    start = (
        b'event: message_start\ndata: {"type":"message_start","message":{"model":'
        b'"claude-opus-5-5","usage":{"input_tokens":10,"cache_read_input_tokens":900,'
        b'"cache_creation_input_tokens":50,"output_tokens":1}}}\n\n'
    )
    for i in range(0, len(start), 7):  # any chunking
        meter.feed_sse(start[i : i + 7])
    meter.feed_sse(b'data: {"type":"message_delta","usage":{"output_tokens":42}}')
    meter.close()

    assert meter.model == "claude-opus-5-5"
    assert meter.usage == {"input": 10, "output": 42, "cache_read": 900, "cache_write": 50}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_stream_usage_with_any_line_ending_and_multi_line_data(newline: str) -> None:
    lines = [
        'data: {"model": "gpt-x",',
        'data: "usage": {"prompt_tokens": 8, "completion_tokens": 1}}',
        "",
        "data: [DONE]",
        "",
    ]
    stream = newline.join(lines).encode()
    meter = UsageMeter()
    for i in range(len(stream)):  # one byte at a time
        meter.feed_sse(stream[i : i + 1])
    meter.close()

    assert meter.model == "gpt-x"
    assert meter.usage == {"input": 8, "output": 1, "cache_read": 0, "cache_write": 0}


def test_openai_counts_the_cached_part_inside_its_input() -> None:
    chat = UsageMeter()
    chat.feed_json(
        {
            "model": "gpt-x",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
        }
    )
    assert chat.usage == {"input": 200, "output": 5, "cache_read": 800, "cache_write": 0}

    responses = UsageMeter()
    responses.feed_sse(
        b'data: {"type":"response.completed","response":{"model":"gpt-x","usage":'
        b'{"input_tokens":100,"output_tokens":5,"input_tokens_details":{"cached_tokens":64}}}}\n'
    )
    responses.close()
    assert responses.usage == {"input": 36, "output": 5, "cache_read": 64, "cache_write": 0}


def test_no_usage_and_an_odd_model_name_are_left_out() -> None:
    meter = UsageMeter()
    meter.feed_json({"model": "name with spaces <alice@example.com>", "choices": []})
    assert meter.usage is None
    assert meter.model is None


@pytest.mark.parametrize(
    "model",
    ["ana@example.com", "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789ABCD", "10.0.0.7"],
)
def test_a_model_id_holding_a_detected_value_is_dropped(model: str) -> None:
    meter = UsageMeter()
    meter.feed_json({"model": model, "usage": {"input_tokens": 1}})
    assert meter.model is None
    assert meter.usage is not None  # the counts are still kept


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5-20251001",
        "claude-3-5-sonnet@20240620",
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "qwen2.5-coder:32b",
    ],
)
def test_real_model_ids_are_kept(model: str) -> None:
    meter = UsageMeter()
    meter.feed_json({"model": model})
    assert meter.model == model


def test_cache_hit_rate() -> None:
    assert cache_hit_rate({"input": 10, "cache_read": 90}) == pytest.approx(0.9)
    assert cache_hit_rate({"output": 5}) is None
