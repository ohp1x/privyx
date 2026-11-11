"""Ollama's native API: request masking and JSON-lines stream restoring."""

from __future__ import annotations

import json
from typing import Any

from privyx.config.schema import ProxyConfig
from privyx.core.engine import PrivacyEngine
from privyx.observability.usage import UsageMeter
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.schemas import detect_schema, restore_response, transform_request
from privyx.proxy.stream_router import LineRouter
from privyx.token.codec import FormatCodec
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
TOKEN = "<PRIVYX_EMAIL_1>"


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


def test_default_routes_cover_the_native_api() -> None:
    routes = ProxyConfig().routes
    assert detect_schema("/api/chat", routes) == "ollama"
    assert detect_schema("api/generate", routes) == "ollama"
    assert detect_schema("/api/embed", routes) == "embeddings"
    assert detect_schema("/api/embeddings", routes) == "embeddings"
    assert detect_schema("/api/tags", routes) is None


async def test_chat_request_masks_messages_but_not_images_or_tool_names() -> None:
    engine = _engine()
    sid = (await engine.get_or_create_session()).session_id
    image = "iVBORw0KGgo" + EMAIL  # base64 is not inspected
    payload = {
        "model": "llama3.2",
        "messages": [
            {"role": "system", "content": f"owner {EMAIL}"},
            {"role": "user", "content": f"mail {EMAIL}", "images": [image]},
            {
                "role": "assistant",
                "content": "",
                "thinking": f"about {EMAIL}",
                "tool_calls": [{"function": {"name": "send", "arguments": {"name": EMAIL}}}],
            },
            {"role": "tool", "content": f"sent to {EMAIL}", "tool_name": "send"},
        ],
        "format": {"type": "object", "enum": [EMAIL]},
        "options": {"stop": [EMAIL]},
    }

    out = await transform_request(payload, engine, sid, schema="ollama")

    messages = out["messages"]
    assert messages[0]["content"] == f"owner {TOKEN}"
    assert messages[1] == {"role": "user", "content": f"mail {TOKEN}", "images": [image]}
    assert messages[2]["thinking"] == f"about {TOKEN}"
    assert messages[2]["tool_calls"][0]["function"] == {
        "name": "send",
        "arguments": {"name": TOKEN},
    }
    assert messages[3] == {"role": "tool", "content": f"sent to {TOKEN}", "tool_name": "send"}
    assert out["format"] == payload["format"]
    assert out["options"] == payload["options"]
    assert await restore_response(out, engine, sid, schema="ollama") == payload


async def test_generate_request_masks_prompt_system_and_suffix() -> None:
    engine = _engine()
    sid = (await engine.get_or_create_session()).session_id
    payload = {
        "model": "qwen2.5-coder",
        "prompt": f"def mail(): # {EMAIL}",
        "suffix": f"return '{EMAIL}'",
        "system": f"for {EMAIL}",
        "images": [EMAIL],
        "context": [1, 2, 3],
    }

    out = await transform_request(payload, engine, sid, schema="ollama")

    assert EMAIL not in json.dumps({k: out[k] for k in ("prompt", "suffix", "system")})
    assert out["images"] == [EMAIL]
    assert out["context"] == [1, 2, 3]


# -- stream ----------------------------------------------------------------


def _chat(content: str = "", *, done: bool = False, **message: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "model": "llama3.2",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": content, **message},
        "done": done,
    }
    if done:
        line.update(done_reason="stop", prompt_eval_count=12, eval_count=5)
    return line


def _run(*objects: dict[str, Any], split: int = 7, tail: str = "\n") -> list[dict[str, Any]]:
    raw = "".join(json.dumps(o) + "\n" for o in objects).rstrip("\n") + tail
    router = LineRouter(FormatCodec.default(), {TOKEN: EMAIL}.get)
    data = raw.encode()
    out = []
    for i in range(0, len(data), split):
        out += router.feed(data[i : i + split])
    out += router.flush()
    text = "".join(out)
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines() if line]


def _field(lines: list[dict[str, Any]], name: str) -> str:
    return "".join((line["message"] if "message" in line else line).get(name, "") for line in lines)


def test_chat_stream_restores_a_token_split_across_lines() -> None:
    lines = _run(
        _chat("Hi " + TOKEN[:6]), _chat(TOKEN[6:12]), _chat(TOKEN[12:] + " bye"), _chat(done=True)
    )

    assert _field(lines, "content") == f"Hi {EMAIL} bye"
    assert TOKEN not in json.dumps(lines)
    assert len(lines) == 4  # a line whose text is held back still goes, empty
    assert lines[-1]["done"] is True and lines[-1]["eval_count"] == 5


def test_chat_stream_done_line_carries_the_held_fragment() -> None:
    lines = _run(_chat("ends with <PRIV"), _chat(done=True))

    assert [line["message"]["content"] for line in lines] == ["ends with ", "<PRIV"]


def test_thinking_is_restored_apart_and_given_back_before_content() -> None:
    lines = _run(
        _chat(thinking="hmm " + TOKEN[:4]),
        _chat(thinking=TOKEN[4:]),
        _chat("to " + TOKEN[:3]),
        _chat(TOKEN[3:]),
        _chat(done=True),
    )

    assert _field(lines, "thinking") == f"hmm {EMAIL}"
    assert _field(lines, "content") == f"to {EMAIL}"


def test_a_thinking_fragment_ends_when_content_starts() -> None:
    lines = _run(_chat(thinking="a <PRIV"), _chat("b"), _chat(done=True))

    assert lines[1]["message"] == {"role": "assistant", "content": "b", "thinking": "<PRIV"}


def test_tool_calls_are_restored_whole_after_the_held_text() -> None:
    call = {"function": {"name": "send", "arguments": {"to": TOKEN, "name": TOKEN}}}
    lines = _run(_chat("to <PRIV"), _chat(tool_calls=[call]), _chat(done=True))

    assert lines[1]["message"]["content"] == "<PRIV"
    assert lines[1]["message"]["tool_calls"] == [
        {"function": {"name": "send", "arguments": {"to": EMAIL, "name": EMAIL}}}
    ]


def test_generate_stream_restores_response_and_thinking() -> None:
    def generate(response: str = "", done: bool = False, **extra: Any) -> dict[str, Any]:
        return {"model": "qwen", "response": response, "done": done, **extra}

    lines = _run(
        generate(thinking="x " + TOKEN[:5]),
        generate("y " + TOKEN[:9]),
        generate(TOKEN[9:]),
        generate(done=True, context=[1, 2]),
    )

    assert _field(lines, "thinking") == f"x {TOKEN[:5]}"  # cut short: never a whole token
    assert _field(lines, "response") == f"y {EMAIL}"
    assert lines[-1]["context"] == [1, 2]


def test_stream_cut_off_ends_with_the_held_text_and_last_line_without_newline() -> None:
    lines = _run(_chat("mail " + TOKEN[:7]), _chat(TOKEN[7:]), tail="")

    assert _field(lines, "content") == f"mail {EMAIL}"

    lines = _run(_chat("mail " + TOKEN[:7]))
    assert lines[-1] == {
        "model": "llama3.2",
        "message": {"role": "assistant", "content": TOKEN[:7]},
        "done": False,
    }


def test_error_and_non_json_lines_come_after_the_held_text() -> None:
    router = LineRouter(FormatCodec.default(), {TOKEN: EMAIL}.get)
    first = json.dumps(_chat("a <PRIV"))
    out = router.feed(f'{first}\nnot json\n{{"error":"failed for {TOKEN}"}}\n')
    out += router.flush()

    assert out[1:] == [
        json.dumps(
            {
                "model": "llama3.2",
                "message": {"role": "assistant", "content": "<PRIV"},
                "done": False,
            },
            separators=(",", ":"),
        )
        + "\n",
        "not json\n",
        f'{{"error":"failed for {EMAIL}"}}\n',
    ]


def test_usage_is_read_from_the_last_line() -> None:
    meter = UsageMeter()
    body = "".join(
        json.dumps(o, separators=(",", ":")) + "\n" for o in (_chat("hi"), _chat(done=True))
    )

    for i in range(0, len(body), 5):
        meter.feed_lines(body[i : i + 5])
    meter.close()

    assert meter.usage == {"input": 12, "output": 5, "cache_read": 0, "cache_write": 0}
    assert meter.model == "llama3.2"


def test_an_error_line_comes_after_the_held_text() -> None:
    lines = _run(_chat("a <PRIV"), {"error": f"failed for {TOKEN}"})

    assert lines[1:] == [
        {"model": "llama3.2", "message": {"role": "assistant", "content": "<PRIV"}, "done": False},
        {"error": f"failed for {EMAIL}"},
    ]


def test_strings_outside_the_message_are_restored_too() -> None:
    lines = _run({**_chat("hi"), "note": f"for {TOKEN}"})

    assert lines[0]["note"] == f"for {EMAIL}"


def test_usage_is_read_from_a_last_line_without_newline() -> None:
    meter = UsageMeter()
    meter.feed_lines(json.dumps(_chat(done=True), separators=(",", ":")))
    meter.close()

    assert meter.usage == {"input": 12, "output": 5, "cache_read": 0, "cache_write": 0}


def test_a_held_fragment_replaces_a_null_text_field() -> None:
    lines = _run(_chat("a <PRIV"), {**_chat(done=True), "message": {"content": None}})

    assert lines[-1]["message"] == {"content": "<PRIV"}
