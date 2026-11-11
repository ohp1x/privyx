"""Ollama's native API through the transparent proxy.

The mock upstream speaks Ollama: ``/api/chat`` and ``/api/generate`` echo the
last user text back, as JSON lines cut into small pieces when ``stream`` is not
``false`` (Ollama's default), else as one JSON object; the chat reply ends with
a tool call that repeats the text.  ``/api/embed`` returns a vector.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"


def _upstream(capture: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        capture.append(request)
        body = json.loads(request.content)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"model": body["model"], "embeddings": [[0.1, 0.2]]})
        chat = request.url.path == "/api/chat"
        text = body["messages"][-1]["content"] if chat else body["prompt"]
        call = {"function": {"name": "note", "arguments": {"name": text}}}
        if body.get("stream") is False:
            message = {"role": "assistant", "content": text, "tool_calls": [call]}
            whole = {"message": message} if chat else {"response": text}
            return httpx.Response(200, json={"model": "llama3.2", **whole, "done": True})
        lines: list[dict[str, Any]] = []
        for i in range(0, len(text), 4):
            piece = text[i : i + 4]
            field = (
                {"message": {"role": "assistant", "content": piece}}
                if chat
                else {"response": piece}
            )
            lines.append({"model": "llama3.2", **field, "done": False})
        if chat:
            message = {"role": "assistant", "content": "", "tool_calls": [call]}
            lines.append({"model": "llama3.2", "message": message, "done": False})
        end = {"message": {"role": "assistant", "content": ""}} if chat else {"response": ""}
        lines.append({"model": "llama3.2", **end, "done": True, "eval_count": 3})
        content = "".join(json.dumps(line) + "\n" for line in lines).encode()
        return httpx.Response(
            200, headers={"content-type": "application/x-ndjson"}, content=content
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


async def _send(path: str, body: dict[str, Any]) -> tuple[Any, str, httpx.Request]:
    capture: list[httpx.Request] = []
    proxy = TransparentProxy(
        _engine(), origin="http://ollama.test:11434", client=_upstream(capture)
    )
    response = await proxy.handle(
        method="POST",
        path=path,
        headers={"content-type": "application/json"},
        body=json.dumps(body).encode(),
    )
    data = response.body
    if data is None:
        assert response.stream is not None
        data = b"".join([c.encode() if isinstance(c, str) else c async for c in response.stream])
    sent = capture[0]
    assert EMAIL not in sent.content.decode()
    return response, data.decode(), sent


def _chat_body(**extra: Any) -> dict[str, Any]:
    return {
        "model": "llama3.2",
        "messages": [{"role": "user", "content": f"mail {EMAIL}"}],
        **extra,
    }


async def test_chat_stream_is_restored_line_by_line() -> None:
    response, body, sent = await _send("api/chat", _chat_body())

    assert response.media_type == "application/x-ndjson"
    assert "<PRIVYX_EMAIL_1>" in sent.content.decode()
    lines = [json.loads(line) for line in body.splitlines()]
    assert "".join(line["message"]["content"] for line in lines) == f"mail {EMAIL}"
    calls = [c for line in lines for c in line["message"].get("tool_calls", [])]
    assert calls == [{"function": {"name": "note", "arguments": {"name": f"mail {EMAIL}"}}}]
    assert lines[-1]["done"] is True


async def test_chat_without_stream_is_restored_whole() -> None:
    _, body, _ = await _send("api/chat", _chat_body(stream=False))

    message = json.loads(body)["message"]
    assert message["content"] == f"mail {EMAIL}"
    assert message["tool_calls"][0]["function"]["arguments"] == {"name": f"mail {EMAIL}"}


async def test_generate_stream_is_restored() -> None:
    _, body, _ = await _send("api/generate", {"model": "llama3.2", "prompt": f"mail {EMAIL}"})

    lines = [json.loads(line) for line in body.splitlines()]
    assert "".join(line["response"] for line in lines) == f"mail {EMAIL}"


async def test_embed_input_is_masked() -> None:
    _, body, sent = await _send("api/embed", {"model": "all-minilm", "input": [f"hi {EMAIL}"]})

    assert json.loads(sent.content)["input"] == ["hi <PRIVYX_EMAIL_1>"]
    assert json.loads(body)["embeddings"] == [[0.1, 0.2]]


def test_the_gateway_does_not_serve_ollama_paths() -> None:
    pytest.importorskip("fastapi")
    from privyx.gateway.server import Gateway
    from privyx.providers.registry import build_provider

    for provider in ("generic", "google"):
        settings = Settings(provider={"type": provider})
        gateway = Gateway(engine=_engine(), provider=build_provider(settings), settings=settings)
        assert "ollama" not in gateway.routes.values()
