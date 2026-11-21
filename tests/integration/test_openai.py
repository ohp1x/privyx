"""Integration tests: OpenAI-shaped traffic through the whole stack.

A stub httpx transport stands in for ``api.openai.com``, so these tests cover
the real :class:`GenericProvider` — request encoding, header handling, SSE
chunking — without a network.  They assert the two things that matter at this
boundary: the upstream never receives PII, and the client never receives a
pseudonym.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from privyx import load_config
from privyx.app._build import build_engine
from privyx.core import Exchange
from privyx.providers import GenericProvider
from privyx.proxy import Pipeline, Reply
from privyx.streaming.sse import SSEDecoder

EMAIL = "alice@example.com"
SSN = "123-45-6789"
URL = "https://api.openai.com/v1/chat/completions"


def openai_stream_body(deltas: list[str]) -> str:
    parts = []
    for delta in deltas:
        payload = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
        }
        parts.append(f"data: {json.dumps(payload, separators=(',', ':'))}\n\n")
    return "".join(parts) + "data: [DONE]\n\n"


def openai_batch_body(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 42},
    }


class Upstream:
    """Records what the upstream received and replays a canned response."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.json_response: dict[str, Any] | None = None
        self.stream_body: str = ""
        self.chunk_size: int = 8
        self.status: int = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "upstream said no"})
        if request.headers.get("accept") == "text/event-stream":
            body = self.stream_body
            size = self.chunk_size

            async def chunks() -> AsyncIterator[bytes]:
                # Chunk on byte boundaries, as a socket would: a multi-byte
                # character can be split across two reads.
                raw = body.encode()
                for i in range(0, len(raw), size):
                    yield raw[i : i + size]

            return httpx.Response(
                200, content=chunks(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=self.json_response or {})

    @property
    def last_payload(self) -> Any:
        return json.loads(self.requests[-1].content)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


#: The session every request of a test is masked in.
SESSION = "ses_openai"


async def build_proxy(upstream: Upstream, **provider_kwargs: Any) -> tuple[Pipeline, Any]:
    """The gateway, posting to ``upstream`` through a client the caller closes."""
    settings = load_config()
    engine, close = await build_engine(settings)
    client = upstream.client()
    proxy = Pipeline(
        engine, GenericProvider(base_url=URL, **provider_kwargs), gateway=True, client=client
    )

    async def close_all() -> None:
        await close()
        await client.aclose()

    return proxy, close_all


async def send(proxy: Pipeline, payload: dict[str, Any]) -> Reply:
    headers = {"x-privyx-session": SESSION}
    body = json.dumps(payload).encode()
    return await proxy.handle(Exchange("POST", "/v1/chat/completions", headers, body))


async def pseudonym_of(proxy: Pipeline, value: str) -> str:
    """The pseudonym ``value`` gets in the session, masked once in a batch request."""
    await send(proxy, {"messages": [{"role": "user", "content": f"mail {value}"}]})
    session = await proxy.engine.vault.get(SESSION)
    assert session is not None
    pseudonym = session.pseudonym_for(value)
    assert pseudonym is not None
    return pseudonym


async def streamed(reply: Reply) -> str:
    assert reply.stream is not None
    return "".join([c if isinstance(c, str) else c.decode() async for c in reply.stream])


def collect_content(raw: str) -> str:
    decoder = SSEDecoder()
    events = decoder.feed(raw) + decoder.flush()
    out = []
    for event in events:
        if event.data == "[DONE]":
            continue
        out.append(json.loads(event.data)["choices"][0]["delta"].get("content") or "")
    return "".join(out)


# --------------------------------------------------------------------------


async def test_streaming_round_trip_hides_pii_from_upstream() -> None:
    """Upstream sees pseudonyms; the client sees the original values back."""
    upstream = Upstream()
    proxy, close = await build_proxy(upstream, api_key="sk-test")
    try:
        email_pseudonym = await pseudonym_of(proxy, EMAIL)
        upstream.stream_body = openai_stream_body(["Sure — ", "mailing ", email_pseudonym, "."])
        out = await streamed(
            await send(
                proxy,
                {
                    "model": "gpt-4o",
                    "stream": True,
                    "messages": [{"role": "user", "content": f"Mail {EMAIL} about SSN {SSN}"}],
                },
            )
        )

        # Upstream never saw the real values...
        sent = json.dumps(upstream.last_payload)
        assert EMAIL not in sent and SSN not in sent
        assert "<PRIVYX_" in sent
        assert upstream.requests[-1].headers["authorization"] == "Bearer sk-test"
        assert upstream.requests[-1].headers["accept"] == "text/event-stream"

        # ...and the client never saw a pseudonym.
        assert "<PRIVYX_" not in out
        assert collect_content(out) == f"Sure — mailing {EMAIL}."
        assert out.endswith("data: [DONE]\n\n")
    finally:
        await close()


@pytest.mark.parametrize("chunk_size", [1, 5, 40, 4096])
async def test_streaming_survives_upstream_chunk_size(chunk_size: int) -> None:
    """The upstream's TCP chunking is not allowed to change the result."""
    upstream = Upstream()
    upstream.chunk_size = chunk_size
    proxy, close = await build_proxy(upstream)
    try:
        pseudonym = await pseudonym_of(proxy, EMAIL)
        upstream.stream_body = openai_stream_body(["to ", pseudonym, " done"])
        out = await streamed(
            await send(proxy, {"messages": [{"role": "user", "content": "hi"}], "stream": True})
        )

        assert collect_content(out) == f"to {EMAIL} done"
    finally:
        await close()


async def test_batch_round_trip() -> None:
    """Non-streaming responses are deanonymized too."""
    upstream = Upstream()
    proxy, close = await build_proxy(upstream)
    try:
        pseudonym = await pseudonym_of(proxy, EMAIL)
        upstream.json_response = openai_batch_body(f"Replied to {pseudonym}")
        reply = await send(
            proxy, {"model": "gpt-4o", "messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )

        assert reply.body is not None
        result = json.loads(reply.body)
        assert result["choices"][0]["message"]["content"] == f"Replied to {EMAIL}"
        assert result["usage"]["total_tokens"] == 42  # untouched fields survive
    finally:
        await close()


async def test_an_upstream_error_reaches_the_client() -> None:
    """An upstream 5xx reaches the client with its status and body, not a raw httpx exception."""
    upstream = Upstream()
    upstream.status = 503
    proxy, close = await build_proxy(upstream)
    try:
        reply = await send(proxy, {"model": "gpt-4o"})

        assert reply.status_code == 503
        assert reply.body is not None and json.loads(reply.body) == {"error": "upstream said no"}
    finally:
        await close()


async def test_the_gateway_closes_only_the_client_it_owns() -> None:
    """A caller-supplied client outlives the gateway; an owned one does not.

    The gateway is closed on shutdown, so one that closed a shared client would
    break every other user of it.
    """
    engine, close = await build_engine(load_config())
    try:
        borrowed = Upstream().client()
        gateway = Pipeline(engine, GenericProvider(base_url=URL), gateway=True, client=borrowed)
        await gateway.close()
        await gateway.close()  # idempotent
        assert not borrowed.is_closed
        await borrowed.aclose()

        owned = Pipeline(engine, GenericProvider(base_url=URL), gateway=True)
        await owned.close()
        assert owned._client.is_closed  # noqa: SLF001
    finally:
        await close()
