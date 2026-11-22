"""Masked requests and restored replies of each API format, against their golden files.

Each format goes through a real ``privyx proxy`` process, the transparent proxy
and the gateway, to a local upstream that records what it receives and answers
with the placeholders it found there.  A case holds what the upstream received
and what the client got back, a batch reply or a stream.

The anchor secret is fixed, so a value gets the same placeholder on every run.
Every proxy masks two terms besides what the built-in patterns find.  The
transparent one keeps the other defaults; the gateways send Privyx's own key
and add the placeholder directive.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import pytest
import yaml

EMAIL = "ana@example.com"
PHONE = "+1 202 555 0143"
HOST = "10.0.0.12"
CARD = "4111 1111 1111 1111"
SECRET = "DB_PASSWORD=hunter2x9"
PERSON = "John Smith"
ORG = "Acme Corp"
#: A value the upstream writes itself, which no request masked.
OTHER = "bob@example.org"

CONFIG = {"detectors": {"regex": {"terms": {"PERSON": [PERSON], "ORG": [ORG]}}}}
#: Headers of the transport and the HTTP client libraries, left out of a record.
_TRANSPORT = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "transfer-encoding",
        "user-agent",
    }
)
_TOKEN = re.compile(r"<PRIVYX_[A-Z_]+_[0-9A-F]+>")

Reply = tuple[int, str, list[bytes]]
Golden = Callable[[str, Any], None]


class Upstream:
    """A local upstream: records each request, and answers it with :attr:`reply`."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.reply: Callable[[dict[str, Any]], Reply] = lambda _: _json({})
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                headers = {k.lower(): v for k, v in self.headers.items()}
                record = {
                    "path": self.path,
                    "headers": {k: headers[k] for k in sorted(headers) if k not in _TRANSPORT},
                    "body": _parsed(raw.decode()),
                }
                upstream.seen.append(record)
                status, content_type, chunks = upstream.reply(record)
                self.send_response(status)
                self.send_header("content-type", content_type)
                if len(chunks) == 1:
                    self.send_header("content-length", str(len(chunks[0])))
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    yield server
    server.close()


@pytest.fixture(scope="module")
def proxies(
    upstream: Upstream, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[dict[str, str]]:
    """The URL of each ``privyx proxy``, started together and stopped at the end."""
    gateway = {**CONFIG, "features": {"directive": True}}
    key = {"PRIVYX_API_KEY": "golden-privyx-key"}
    setups = {
        "transparent": (["--transparent", "-u", upstream.url], CONFIG, {}),
        # The key's URL beside it: Privyx's key goes nowhere else.
        "gateway-openai": (
            ["--gateway"],
            gateway,
            {
                "PRIVYX_PROVIDER": "openai",
                **key,
                "PRIVYX_UPSTREAM_URL": upstream.url + "/v1/chat/completions",
            },
        ),
        "gateway-anthropic": (
            ["--gateway"],
            gateway,
            {
                "PRIVYX_PROVIDER": "anthropic",
                **key,
                "PRIVYX_UPSTREAM_URL": upstream.url + "/v1/messages",
            },
        ),
        "gateway-google": (
            ["--gateway"],
            gateway,
            {"PRIVYX_PROVIDER": "google", **key, "PRIVYX_UPSTREAM_URL": upstream.url},
        ),
    }
    started: dict[str, tuple[subprocess.Popen[bytes], str, Path]] = {}
    try:
        for name, (args, config, env) in setups.items():
            started[name] = _start(tmp_path_factory.mktemp(name), args, config, env)
        for process, url, log in started.values():
            _wait_until_up(process, url, log)
        yield {name: url for name, (_, url, _) in started.items()}
    finally:
        for process, _, _ in started.values():
            process.terminate()
        for process, _, _ in started.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def _start(
    root: Path, args: list[str], config: dict[str, Any], env: dict[str, str]
) -> tuple[subprocess.Popen[bytes], str, Path]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    base = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    environment = {
        **base,
        "HOME": str(root),
        "USERPROFILE": str(root),
        "PRIVYX_ANCHOR_SECRET": "golden-anchor",
        **env,
    }
    command = [sys.executable, "-m", "privyx.cli.main", "-c", "config.yaml", "proxy"]
    log = root / "proxy.log"
    with log.open("wb") as out:
        process = subprocess.Popen(
            [*command, "--port", str(port), *args],
            cwd=root,
            env=environment,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    return process, f"http://127.0.0.1:{port}", log


def _wait_until_up(process: subprocess.Popen[bytes], url: str, log: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if httpx.get(url + "/health", trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    process.kill()
    raise AssertionError(f"privyx proxy did not start:\n{log.read_text()}")


def _parsed(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _json(body: Any) -> Reply:
    return 200, "application/json", [json.dumps(body).encode()]


def _sse(events: list[tuple[str | None, Any]]) -> Reply:
    frames = []
    for name, data in events:
        line = data if isinstance(data, str) else json.dumps(data)
        frames.append(((f"event: {name}\n" if name else "") + f"data: {line}\n\n").encode())
    return 200, "text/event-stream", frames


def _lines(objects: list[Any]) -> Reply:
    return 200, "application/x-ndjson", [(json.dumps(o) + "\n").encode() for o in objects]


def _tokens(record: dict[str, Any]) -> list[str]:
    """The placeholders of a recorded request, each once, in order."""
    return list(dict.fromkeys(_TOKEN.findall(json.dumps(record, ensure_ascii=False))))


def _noted(record: dict[str, Any]) -> str:
    return "Noted: " + " ".join(_tokens(record)) + "."


def _pieces(text: str, size: int = 7) -> list[str]:
    """``text`` cut every ``size`` characters: a placeholder spans several pieces."""
    return [text[at : at + size] for at in range(0, len(text), size)]


def _events(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event: dict[str, Any] = {}
        data = []
        for line in block.splitlines():
            field, _, value = line.partition(":")
            if field == "event":
                event["event"] = value.strip()
            elif field == "data":
                data.append(value.removeprefix(" "))
        if data:
            events.append({**event, "data": _parsed("\n".join(data))})
    return events


def _send(
    url: str,
    upstream: Upstream,
    path: str,
    body: dict[str, Any],
    reply: Callable[[dict[str, Any]], Reply],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One exchange: what the upstream received, and what the client got back."""
    upstream.seen.clear()
    upstream.reply = reply
    sent = {"user-agent": "golden-client", "x-acme-trace": "golden", **(headers or {})}
    with httpx.Client(base_url=url, trust_env=False, timeout=30) as client:
        response = client.post(path, json=body, headers=sent)
    kind = response.headers.get("content-type", "").partition(";")[0]
    if kind == "text/event-stream":
        got: Any = _events(response.text)
    elif kind == "application/x-ndjson":
        got = [json.loads(line) for line in response.text.splitlines() if line]
    else:
        got = _parsed(response.text)
    return {
        "upstream": list(upstream.seen),
        "response": {"status": response.status_code, "content_type": kind, "body": got},
    }


# -- OpenAI Chat Completions and legacy completions ---------------------------------


def _chat(stream: bool) -> dict[str, Any]:
    return {
        "model": "gpt-golden",
        "messages": [
            {"role": "system", "content": f"You help {PERSON} of {ORG}."},
            {"role": "user", "content": f"Mail {EMAIL} or call {PHONE}."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": json.dumps({"email": EMAIL, "host": HOST}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": f"{SECRET} on {HOST}"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Card {CARD} is {PERSON}'s."},
                    {"type": "image_url", "image_url": {"url": "https://example.com/receipt.png"}},
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": f"Find a customer of {ORG}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string", "description": f"Such as {EMAIL}"}
                        },
                    },
                },
            }
        ],
        "metadata": {"run": "golden"},
        "stream": stream,
    }


def _chat_reply(record: dict[str, Any]) -> Reply:
    text = _noted(record)
    arguments = json.dumps({"email": _tokens(record)[0]})
    call = {"id": "call_2", "type": "function", "function": {"name": "lookup"}}
    if not record["body"].get("stream"):
        message = {
            "role": "assistant",
            "content": text,
            "tool_calls": [{**call, "function": {**call["function"], "arguments": arguments}}],
        }
        return _json(
            {
                "id": "chatcmpl-golden",
                "object": "chat.completion",
                "model": "gpt-golden",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
            }
        )
    base = {"id": "chatcmpl-golden", "object": "chat.completion.chunk", "model": "gpt-golden"}

    def chunk(delta: dict[str, Any], finish: str | None = None) -> tuple[None, Any]:
        return None, {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    first = {**call, "index": 0, "function": {**call["function"], "arguments": ""}}
    return _sse(
        [
            chunk({"role": "assistant", "content": ""}),
            *[chunk({"content": piece}) for piece in _pieces(text)],
            chunk({"tool_calls": [first]}),
            *[
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
                for piece in _pieces(arguments)
            ],
            chunk({}, "tool_calls"),
            (None, {**base, "choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9}}),
            (None, "[DONE]"),
        ]
    )


def _completion_reply(record: dict[str, Any]) -> Reply:
    choice = {"index": 0, "text": _noted(record), "finish_reason": "stop"}
    return _json({"id": "cmpl-golden", "object": "text_completion", "choices": [choice]})


def test_openai(proxies: dict[str, str], upstream: Upstream, golden: Golden) -> None:
    auth = {"authorization": "Bearer golden-client-key"}
    completion = {
        "model": "gpt-golden-instruct",
        "prompt": f"Write to {PERSON} at {EMAIL}:",
        "suffix": f"Regards, {ORG}",
        "max_tokens": 16,
    }
    leak = {"model": "gpt-golden", "messages": [{"role": "user", "content": EMAIL}], "user": EMAIL}
    cases = {}
    for mode in ("transparent", "gateway-openai"):
        url = proxies[mode]
        cases[mode] = {
            "chat": _send(url, upstream, "/v1/chat/completions", _chat(False), _chat_reply, auth),
            "chat-stream": _send(
                url, upstream, "/v1/chat/completions", _chat(True), _chat_reply, auth
            ),
            "completions": _send(
                url, upstream, "/v1/completions", completion, _completion_reply, auth
            ),
            # The email is masked in the message, and sent as is in `user`.
            "leak": _send(url, upstream, "/v1/chat/completions", leak, _chat_reply, auth),
        }
    golden("masking/openai", cases)


# -- Embeddings -----------------------------------------------------------------------


def _vectors(_: dict[str, Any]) -> Reply:
    data = [{"object": "embedding", "index": 0, "embedding": [0.1, -0.2]}]
    return _json({"object": "list", "data": data, "model": "embed-golden"})


def test_embeddings(proxies: dict[str, str], upstream: Upstream, golden: Golden) -> None:
    auth = {"authorization": "Bearer golden-client-key"}
    text = {"model": "embed-golden", "input": [f"{PERSON} of {ORG}", EMAIL]}
    ids = {"model": "embed-golden", "input": [[101, 2023]]}
    cases = {}
    for mode in ("transparent", "gateway-openai"):
        url = proxies[mode]
        cases[mode] = {
            "text": _send(url, upstream, "/v1/embeddings", text, _vectors, auth),
            # Token ids are not text to mask: refused.
            "token-ids": _send(url, upstream, "/v1/embeddings", ids, _vectors, auth),
        }
    url = proxies["transparent"]
    embed = {"model": "llama-golden", "input": [PERSON, EMAIL]}
    legacy = {"model": "llama-golden", "prompt": f"{PERSON} at {EMAIL}"}
    cases["transparent"]["ollama-embed"] = _send(
        url, upstream, "/api/embed", embed, lambda _: _json({"embeddings": [[0.1, -0.2]]})
    )
    cases["transparent"]["ollama-embeddings"] = _send(
        url, upstream, "/api/embeddings", legacy, lambda _: _json({"embedding": [0.1, -0.2]})
    )
    golden("masking/embeddings", cases)


# -- Anthropic Messages -----------------------------------------------------------------


def _anthropic(messages: list[Any], stream: bool) -> dict[str, Any]:
    return {
        "model": "claude-golden",
        "max_tokens": 64,
        "system": [{"type": "text", "text": f"You help {PERSON} of {ORG}."}],
        "messages": messages,
        "tools": [
            {
                "name": "send_mail",
                "description": f"Send mail for the staff of {ORG}",
                "input_schema": {"type": "object", "properties": {"to": {"type": "string"}}},
            }
        ],
        "metadata": {"user_id": "user_golden"},
        "safeguards": {"cwd": "/srv/acme/apollo", "git_email": EMAIL},
        "stream": stream,
    }


_ANTHROPIC_TURNS = [
    {"role": "user", "content": f"Mail {EMAIL} about the outage on {HOST}."},
    {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "send_mail",
                "input": {"to": EMAIL, "name": PERSON, "retries": 3},
            }
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [{"type": "text", "text": f"{SECRET} was used"}],
            },
            {"type": "text", "text": f"Then call {PHONE}."},
            {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": f"Invoice of {ORG}, card {CARD}",
                },
            },
        ],
    },
]


def _anthropic_reply(record: dict[str, Any]) -> Reply:
    first = _tokens(record)[0]
    thinking = f"{OTHER} can wait; first {first}."
    text = _noted(record)
    arguments = json.dumps({"to": first})
    if not record["body"].get("stream"):
        content = [
            {"type": "thinking", "thinking": thinking, "signature": "sig-golden"},
            {"type": "text", "text": text},
            {"type": "tool_use", "id": "toolu_2", "name": "send_mail", "input": {"to": first}},
        ]
        return _json(
            {
                "id": "msg_golden",
                "type": "message",
                "role": "assistant",
                "model": "claude-golden",
                "content": content,
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 40, "output_tokens": 9},
            }
        )

    def delta(index: int, kind: str, field: str, piece: str) -> tuple[str, Any]:
        body = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": kind, field: piece},
        }
        return "content_block_delta", body

    def block(index: int, start: dict[str, Any]) -> tuple[str, Any]:
        return "content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": start,
        }

    def stop(index: int) -> tuple[str, Any]:
        return "content_block_stop", {"type": "content_block_stop", "index": index}

    message = {
        "id": "msg_golden_stream",
        "type": "message",
        "role": "assistant",
        "model": "claude-golden",
        "content": [],
        "stop_reason": None,
        "usage": {"input_tokens": 40, "output_tokens": 1},
    }
    tool = {"type": "tool_use", "id": "toolu_3", "name": "send_mail", "input": {}}
    return _sse(
        [
            ("message_start", {"type": "message_start", "message": message}),
            block(0, {"type": "thinking", "thinking": "", "signature": ""}),
            *[delta(0, "thinking_delta", "thinking", piece) for piece in _pieces(thinking)],
            delta(0, "signature_delta", "signature", "sig-golden-stream"),
            stop(0),
            block(1, {"type": "text", "text": ""}),
            *[delta(1, "text_delta", "text", piece) for piece in _pieces(text)],
            stop(1),
            block(2, tool),
            *[delta(2, "input_json_delta", "partial_json", piece) for piece in _pieces(arguments)],
            stop(2),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 9},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def test_anthropic(proxies: dict[str, str], upstream: Upstream, golden: Golden) -> None:
    auth = {"x-api-key": "golden-client-key"}
    cases: dict[str, dict[str, Any]] = {}
    for mode in ("transparent", "gateway-anthropic"):
        url = proxies[mode]
        batch = _send(
            url,
            upstream,
            "/v1/messages",
            _anthropic(_ANTHROPIC_TURNS, False),
            _anthropic_reply,
            auth,
        )
        # The next turn echoes the reply as the client got it, its thinking
        # included: the upstream gets back the thinking text it signed.
        reply = {"role": "assistant", "content": batch["response"]["body"].get("content", [])}
        turns = [*_ANTHROPIC_TURNS, reply, {"role": "user", "content": f"Thanks, {PERSON}."}]
        cases[mode] = {
            "messages": batch,
            "messages-stream": _send(
                url, upstream, "/v1/messages", _anthropic(turns, True), _anthropic_reply, auth
            ),
        }
    count = {"model": "claude-golden", "messages": [{"role": "user", "content": f"Mail {EMAIL}"}]}
    cases["transparent"]["count-tokens"] = _send(
        proxies["transparent"],
        upstream,
        "/v1/messages/count_tokens",
        count,
        lambda _: _json({"input_tokens": 12}),
        auth,
    )
    golden("masking/anthropic", cases)


# -- OpenAI Responses -----------------------------------------------------------------


def _responses(stream: bool) -> dict[str, Any]:
    return {
        "model": "gpt-golden",
        "instructions": f"Answer for {PERSON} of {ORG}.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"Mail {EMAIL} about {HOST}."}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": json.dumps({"email": EMAIL}),
            },
            {"type": "function_call_output", "call_id": "call_1", "output": SECRET},
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": f"{PERSON} asked."}],
                "encrypted_content": "gAAAA-golden",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": f"Find a person at {ORG}",
                "parameters": {"type": "object", "properties": {"email": {"type": "string"}}},
            }
        ],
        "client_metadata": {"workspace": "/srv/acme/apollo", "remote": f"git@{HOST}:acme.git"},
        "store": False,
        "stream": stream,
    }


def _responses_reply(record: dict[str, Any]) -> Reply:
    first = _tokens(record)[0]
    text = _noted(record)
    arguments = json.dumps({"email": first})
    summary = {"type": "summary_text", "text": f"About {first}."}
    message = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    call = {
        "type": "function_call",
        "id": "fc_2",
        "call_id": "call_2",
        "name": "lookup",
        "arguments": arguments,
        "status": "completed",
    }
    output = [{"type": "reasoning", "id": "rs_2", "summary": [summary]}, message, call]
    done = {"id": "resp_golden", "object": "response", "status": "completed", "output": output}
    if not record["body"].get("stream"):
        return _json({**done, "model": "gpt-golden"})
    text_ids = {"item_id": "msg_1", "output_index": 1, "content_index": 0}
    call_ids = {"item_id": "fc_2", "output_index": 2}

    def event(kind: str, **fields: Any) -> tuple[str, Any]:
        return kind, {"type": kind, **fields}

    return _sse(
        [
            event("response.created", response={**done, "status": "in_progress", "output": []}),
            event("response.output_item.added", output_index=1, item={**message, "content": []}),
            *[
                event("response.output_text.delta", delta=piece, **text_ids)
                for piece in _pieces(text)
            ],
            event("response.output_text.done", text=text, **text_ids),
            event("response.output_item.done", output_index=1, item=message),
            event("response.output_item.added", output_index=2, item={**call, "arguments": ""}),
            *[
                event("response.function_call_arguments.delta", delta=piece, **call_ids)
                for piece in _pieces(arguments)
            ],
            event("response.function_call_arguments.done", arguments=arguments, **call_ids),
            event("response.output_item.done", output_index=2, item=call),
            event("response.completed", response=done),
        ]
    )


def test_responses(proxies: dict[str, str], upstream: Upstream, golden: Golden) -> None:
    headers = {
        "authorization": "Bearer golden-client-key",
        "x-codex-turn-metadata": json.dumps({"cwd": "/srv/acme/apollo", "email": EMAIL}),
    }
    cases = {}
    for mode in ("transparent", "gateway-openai"):
        url = proxies[mode]
        cases[mode] = {
            "responses": _send(
                url, upstream, "/v1/responses", _responses(False), _responses_reply, headers
            ),
            "responses-stream": _send(
                url, upstream, "/v1/responses", _responses(True), _responses_reply, headers
            ),
        }
    count = {"model": "gpt-golden", "input": f"Mail {EMAIL} for {PERSON}"}
    cases["transparent"]["input-tokens"] = _send(
        proxies["transparent"],
        upstream,
        "/v1/responses/input_tokens",
        count,
        lambda _: _json({"object": "response.input_tokens", "input_tokens": 12}),
        headers,
    )
    golden("masking/responses", cases)


# -- Gemini -----------------------------------------------------------------------------


def _gemini() -> dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": f"You help {PERSON} of {ORG}."}]},
        "contents": [
            {"role": "user", "parts": [{"text": f"Mail {EMAIL} about {HOST}."}]},
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": "send_mail",
                            "args": {"to": EMAIL, "name": PERSON},
                        },
                        "thoughtSignature": "c2lnLWdvbGRlbg==",
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "send_mail",
                            "response": {"result": f"sent from {PHONE}"},
                        }
                    },
                    {
                        "fileData": {
                            "mimeType": "text/plain",
                            "fileUri": "https://example.com/files/acme",
                        }
                    },
                ],
            },
        ],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "send_mail",
                        "description": f"Mail the staff of {ORG}",
                        "parameters": {"type": "OBJECT", "properties": {"to": {"type": "STRING"}}},
                    }
                ]
            }
        ],
        "generationConfig": {"temperature": 0},
    }


def _gemini_reply(record: dict[str, Any]) -> Reply:
    first = _tokens(record)[0]
    call = {"functionCall": {"name": "send_mail", "args": {"to": first}}}
    usage = {"promptTokenCount": 40, "candidatesTokenCount": 9, "totalTokenCount": 49}
    ids = {"modelVersion": "gemini-golden", "responseId": "golden"}
    if ":streamGenerateContent" not in record["path"]:
        content = {"role": "model", "parts": [{"text": _noted(record)}, call]}
        candidate = {"content": content, "finishReason": "STOP", "index": 0}
        return _json({"candidates": [candidate], "usageMetadata": usage, **ids})

    def chunk(parts: list[Any], **end: Any) -> tuple[None, Any]:
        candidate = {"content": {"role": "model", "parts": parts}, "index": 0, **end}
        return None, {"candidates": [candidate], **ids}

    return _sse(
        [
            *[chunk([{"text": piece}]) for piece in _pieces(_noted(record))],
            (None, {**chunk([call], finishReason="STOP")[1], "usageMetadata": usage}),
        ]
    )


def test_gemini(proxies: dict[str, str], upstream: Upstream, golden: Golden) -> None:
    model = "/v1beta/models/gemini-golden"
    count = {"generateContentRequest": {"model": "models/gemini-golden", **_gemini()}}
    cases = {}
    for mode, auth in (("transparent", "?key=golden-client-key"), ("gateway-google", "")):
        url = proxies[mode]
        stream = auth + ("&" if auth else "?") + "alt=sse"
        cases[mode] = {
            "generate": _send(
                url, upstream, f"{model}:generateContent{auth}", _gemini(), _gemini_reply
            ),
            "generate-stream": _send(
                url, upstream, f"{model}:streamGenerateContent{stream}", _gemini(), _gemini_reply
            ),
            "count-tokens": _send(
                url,
                upstream,
                f"{model}:countTokens{auth}",
                count,
                lambda _: _json({"totalTokens": 12}),
            ),
        }
    golden("masking/gemini", cases)


# -- Ollama -----------------------------------------------------------------------------


def _ollama_chat(stream: bool) -> dict[str, Any]:
    return {
        "model": "llama-golden",
        "messages": [
            {"role": "system", "content": f"You help {PERSON} of {ORG}."},
            {"role": "user", "content": f"Mail {EMAIL} about {HOST}.", "images": ["aGVsbG8="]},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "send_mail", "arguments": {"to": EMAIL}}}],
            },
            {"role": "tool", "tool_name": "send_mail", "content": f"sent from {PHONE}"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "send_mail",
                    "description": f"Mail the staff of {ORG}",
                    "parameters": {"type": "object", "properties": {"to": {"type": "string"}}},
                },
            }
        ],
        "options": {"temperature": 0},
        "stream": stream,
    }


def _ollama_reply(record: dict[str, Any]) -> Reply:
    first = _tokens(record)[0]
    text = _noted(record)
    base = {"model": "llama-golden", "created_at": "2026-01-01T00:00:00Z"}
    chat = record["path"] == "/api/chat"
    calls = [{"function": {"name": "send_mail", "arguments": {"to": first}}}]
    end = {**base, "done": True, "done_reason": "stop", "eval_count": 9}

    def part(piece: str) -> dict[str, Any]:
        if chat:
            return {**base, "message": {"role": "assistant", "content": piece}, "done": False}
        return {**base, "response": piece, "done": False}

    if not record["body"].get("stream", True):
        if chat:
            message = {"role": "assistant", "content": text, "tool_calls": calls}
            return _json({**end, "message": message})
        return _json({**end, "response": text})
    lines = [part(piece) for piece in _pieces(text)]
    if chat:
        call = {"role": "assistant", "content": "", "tool_calls": calls}
        lines.append({**base, "message": call, "done": False})
        lines.append({**end, "message": {"role": "assistant", "content": ""}})
    else:
        lines.append({**end, "response": ""})
    return _lines(lines)


def test_ollama(proxies: dict[str, str], upstream: Upstream, golden: Golden) -> None:
    url = proxies["transparent"]
    generate = {
        "model": "llama-golden",
        "system": f"You help {PERSON} of {ORG}.",
        "prompt": f"Mail {EMAIL} about {HOST}.",
    }
    golden(
        "masking/ollama",
        {
            "transparent": {
                "chat": _send(url, upstream, "/api/chat", _ollama_chat(False), _ollama_reply),
                "chat-stream": _send(url, upstream, "/api/chat", _ollama_chat(True), _ollama_reply),
                "generate": _send(
                    url, upstream, "/api/generate", {**generate, "stream": False}, _ollama_reply
                ),
                "generate-stream": _send(url, upstream, "/api/generate", generate, _ollama_reply),
            }
        },
    )
