"""``proxy.response`` carries the token usage the upstream reported."""

from __future__ import annotations

import io
import json

import httpx

from privyx.core.engine import PrivacyEngine
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

USAGE = {
    "input_tokens": 12,
    "cache_read_input_tokens": 3000,
    "cache_creation_input_tokens": 40,
    "output_tokens": 7,
}
BODY = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}


def _upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/embeddings":
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-3-small",
                "data": [{"embedding": [0.1]}],
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            },
        )
    if json.loads(request.content).get("stream"):
        events = [
            {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": USAGE}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "message_delta", "usage": {"output_tokens": 9}},
        ]
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)
    return httpx.Response(
        200,
        json={
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "ok"}],
            "usage": USAGE,
        },
    )


def _proxy(buf: io.StringIO) -> TransparentProxy:
    audit = AuditLogger(buf)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    return TransparentProxy(engine, origin="https://up.test", client=client, audit=audit)


def _response(buf: io.StringIO) -> dict:
    records = [json.loads(line) for line in buf.getvalue().splitlines()]
    return next(r for r in records if r["event"] == "proxy.response")


async def test_batch_response_records_usage_and_model() -> None:
    buf = io.StringIO()
    proxy = _proxy(buf)
    await proxy.handle(
        method="POST", path="v1/messages", headers={}, body=json.dumps(BODY).encode()
    )

    response = _response(buf)
    assert response["model"] == "claude-opus-5-5"
    assert response["usage"] == {"input": 12, "output": 7, "cache_read": 3000, "cache_write": 40}
    assert proxy.stats.tokens["cache_read"] == 3000
    assert 'privyx_tokens_total{kind="cache_read"} 3000' in proxy.stats.prometheus()


async def test_embeddings_response_records_usage() -> None:
    buf = io.StringIO()
    proxy = _proxy(buf)
    body = {"model": "text-embedding-3-small", "input": "hi"}
    await proxy.handle(
        method="POST", path="v1/embeddings", headers={}, body=json.dumps(body).encode()
    )

    response = _response(buf)
    assert response["model"] == "text-embedding-3-small"
    assert response["usage"] == {"input": 8, "output": 0, "cache_read": 0, "cache_write": 0}


async def test_stream_response_records_the_final_usage() -> None:
    buf = io.StringIO()
    proxy = _proxy(buf)
    result = await proxy.handle(
        method="POST",
        path="v1/messages",
        headers={},
        body=json.dumps({**BODY, "stream": True}).encode(),
    )
    assert result.stream is not None
    _ = [chunk async for chunk in result.stream]

    assert _response(buf)["usage"] == {
        "input": 12,
        "output": 9,
        "cache_read": 3000,
        "cache_write": 40,
    }
