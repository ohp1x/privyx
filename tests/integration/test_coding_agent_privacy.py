"""Synthetic coding-agent exchanges through the ASGI app; no CLI or API key needed.

Each content surface has its own canary. The upstream echoes only the tokens
it actually received, as both text and tool arguments, so the checks detect
missing masking, missing restore, and dropped content independently.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import httpx
import pytest

from privyx.config.loader import load_config
from privyx.core.builder import build_engine
from privyx.gateway.transparent import create_transparent_app
from privyx.observability.audit import AuditLogger
from privyx.proxy.transparent import TransparentProxy
from privyx.streaming.sse import SSEDecoder

TOKEN = re.compile(r"<PRIVYX_[A-Z_]+_[A-Za-z0-9]+>")
PATHS = {
    "anthropic": "/v1/messages",
    "responses": "/v1/responses",
    "openai": "/v1/chat/completions",
}
CANARIES = (
    "system-canary@example.test",
    "prompt-canary@example.test",
    "description-canary@example.test",
    "parameter-canary@example.test",
    "typed-source-canary-739",
    "triple-source-canary-482\nsecond-line-canary-815",
    "dotenv-canary-601",
    "shell-canary-918",
    "log-canary-247",
    "machine-canary@example.test",
)
SOURCE = (
    f'password: str = "{CANARIES[4]}"\n'
    f'SECRET_KEY = """{CANARIES[5]}"""\n'
    f"DB_PASSWORD={CANARIES[6]}\n"
    f"2026-10-16T09:49:08.123+08:00 password={CANARIES[8]}\n"
)


def _request(schema: str, stream: bool) -> tuple[dict[str, Any], dict[str, str]]:
    description = {
        "name": "lookup",
        "description": CANARIES[2],
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": CANARIES[3]}},
        },
    }
    command = f"redis-server --requirepass {CANARIES[7]}"
    payload: dict[str, Any] = {"model": "local-test", "stream": stream}
    if schema == "anthropic":
        description["input_schema"] = description.pop("parameters")
        payload.update(
            max_tokens=100,
            system=[{"type": "text", "text": CANARIES[0]}],
            tools=[description],
            safeguards={"cwd": f"/home/{CANARIES[9]}/work"},
            messages=[
                {"role": "user", "content": CANARIES[1]},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "lookup",
                            "input": {"command": command},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": SOURCE},
                    ],
                },
            ],
        )
    elif schema == "responses":
        payload.update(
            instructions=CANARIES[0],
            tools=[{"type": "function", **description}],
            input=[
                {"role": "user", "content": [{"type": "input_text", "text": CANARIES[1]}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": json.dumps({"command": command}),
                },
                {"type": "function_call_output", "call_id": "call_1", "output": SOURCE},
            ],
        )
    else:
        payload.update(
            tools=[{"type": "function", "function": description}],
            messages=[
                {"role": "system", "content": CANARIES[0]},
                {"role": "user", "content": CANARIES[1]},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": json.dumps({"command": command}),
                            },
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": SOURCE},
            ],
        )
    headers = {"authorization": "Bearer dummy-client-credential"}
    if schema != "anthropic":
        metadata = json.dumps({"workspaces": {f"/home/{CANARIES[9]}/work": {"status": "ok"}}})
        payload["client_metadata"] = {"x-codex-turn-metadata": metadata}
        headers["x-codex-turn-metadata"] = metadata
    return payload, headers


def _reply(schema: str, tokens: list[str], stream: bool) -> httpx.Response:
    text, arguments = " | ".join(tokens), json.dumps({"values": tokens})
    if schema == "anthropic":
        batch = {
            "content": [
                {"type": "text", "text": text},
                {"type": "tool_use", "id": "call_2", "name": "lookup", "input": {"values": tokens}},
            ]
        }
        events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            *(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": part},
                }
                for part in _parts(text)
            ),
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_2",
                    "name": "lookup",
                    "input": {},
                },
            },
            *(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": part},
                }
                for part in _parts(arguments)
            ),
            {"type": "content_block_stop", "index": 1},
            {"type": "message_stop"},
        ]
    elif schema == "responses":
        batch = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
                {"type": "function_call", "name": "lookup", "arguments": arguments},
            ]
        }
        events = [
            *(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": part,
                }
                for part in _parts(text)
            ),
            {
                "type": "response.output_text.done",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
            *(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "call_2",
                    "output_index": 1,
                    "delta": part,
                }
                for part in _parts(arguments)
            ),
            {
                "type": "response.function_call_arguments.done",
                "item_id": "call_2",
                "output_index": 1,
                "arguments": arguments,
            },
            {"type": "response.completed", "response": batch},
        ]
    else:
        batch = {
            "choices": [
                {
                    "message": {
                        "content": text,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {"name": "lookup", "arguments": arguments},
                            },
                        ],
                    }
                }
            ]
        }
        events = [
            *({"choices": [{"delta": {"content": part}}]} for part in _parts(text)),
            *(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_2",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup" if i == 0 else "",
                                            "arguments": part,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
                for i, part in enumerate(_parts(arguments))
            ),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    if not stream:
        return httpx.Response(200, json=batch)
    frames = "".join(
        f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n" for event in events
    )
    if schema == "openai":
        frames += "data: [DONE]\n\n"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_Chunks(frames.encode())
    )


def _parts(text: str) -> list[str]:
    return [text[i : i + 5] for i in range(0, len(text), 5)]


class _Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        for i in range(0, len(self.content), 17):
            yield self.content[i : i + 17]

    def __init__(self, content: bytes) -> None:
        self.content = content


@pytest.mark.parametrize("schema", PATHS)
@pytest.mark.parametrize("stream", [False, True], ids=["batch", "stream"])
@pytest.mark.parametrize("strategy", ["ephemeral", "conversation"])
async def test_agent_content_is_masked_and_restored(
    schema: str, stream: bool, strategy: str
) -> None:
    audit_file = io.StringIO()
    audit = AuditLogger(audit_file)
    engine, close = await build_engine(load_config(None), audit=audit)
    seen: list[dict[str, Any]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        forwarded = {"body": body, "headers": dict(request.headers)}
        serialized = json.dumps(forwarded)
        for canary in CANARIES:
            for line in canary.splitlines():
                assert line not in serialized, f"upstream leak: {line}"
        tokens = list(dict.fromkeys(TOKEN.findall(serialized)))
        assert len(tokens) == len(CANARIES), "each content surface must arrive masked"
        seen.append(body)
        return _reply(schema, tokens, stream)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as provider:
            proxy = TransparentProxy(
                engine,
                origin="https://upstream.test",
                client=provider,
                audit=audit,
                session_strategy=strategy,
            )
            app = create_transparent_app(engine, proxy)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://proxy.test"
            ) as client:
                # Resend a second turn to exercise the detection cache and session reuse.
                for _ in range(2):
                    payload, headers = _request(schema, stream)
                    response = await client.post(PATHS[schema], json=payload, headers=headers)
                    assert response.status_code == 200
                    assert "PRIVYX_" not in response.text
                    if stream:
                        events = SSEDecoder().feed(response.text)
                        bodies = [json.loads(e.data) for e in events if e.data != "[DONE]"]
                        if schema == "anthropic":
                            deltas = [
                                b["delta"] for b in bodies if b["type"] == "content_block_delta"
                            ]
                            text = "".join(d.get("text", "") for d in deltas)
                            args = "".join(d.get("partial_json", "") for d in deltas)
                        elif schema == "responses":
                            text = "".join(
                                b["delta"]
                                for b in bodies
                                if b["type"] == "response.output_text.delta"
                            )
                            args = next(
                                b["arguments"]
                                for b in bodies
                                if b["type"] == "response.function_call_arguments.done"
                            )
                        else:
                            deltas = [b["choices"][0]["delta"] for b in bodies]
                            text = "".join(d.get("content", "") for d in deltas)
                            args = "".join(
                                t["function"].get("arguments", "")
                                for d in deltas
                                for t in d.get("tool_calls", [])
                            )
                    else:
                        body = response.json()
                        if schema == "anthropic":
                            text = body["content"][0]["text"]
                            args = json.dumps(body["content"][1]["input"])
                        elif schema == "responses":
                            text = body["output"][0]["content"][0]["text"]
                            args = body["output"][1]["arguments"]
                        else:
                            message = body["choices"][0]["message"]
                            text = message["content"]
                            args = message["tool_calls"][0]["function"]["arguments"]
                    values = json.loads(args)["values"]
                    assert set(values) == set(CANARIES)
                    assert text == " | ".join(values)
                assert len(seen) == 2
                prefix = "system" if schema == "anthropic" else "instructions"
                if schema != "openai":
                    assert seen[0][prefix] == seen[1][prefix]
                assert seen[0]["tools"] == seen[1]["tools"]
        records = [json.loads(line) for line in audit_file.getvalue().splitlines()]
        assert sum(r["event"] == "proxy.response" for r in records) == 2
        assert not any(r["event"] == "proxy.error" for r in records)
        for canary in CANARIES:
            for line in canary.splitlines():
                assert line not in audit_file.getvalue(), "audit must not contain original values"
        if strategy == "ephemeral":
            assert await engine.vault.list_sessions() == []
    finally:
        await close()
