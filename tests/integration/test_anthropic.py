"""Integration tests: Anthropic-shaped traffic through the whole stack.

Anthropic's stream differs from OpenAI's in ways the proxy has to respect: the
text lives in ``content_block_delta`` events, named events carry an ``event:``
line that must survive rewriting, and several event types in the same stream
are not text at all.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from privyx.config.loader import load_config
from privyx.core.builder import build_engine
from privyx.proxy.http import HTTPProxy
from privyx.streaming.adapters.registry import build_stream_adapter
from privyx.streaming.sse import SSEDecoder, SSEEvent

EMAIL = "alice@example.com"
PHONE = "+1-555-0142"
URL = "https://api.anthropic.com/v1/messages"


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def anthropic_stream(deltas: list[str]) -> str:
    """A full message stream: start → block → deltas → stop."""
    parts = [
        _event("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
        _event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        ),
    ]
    parts += [
        _event(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": d}},
        )
        for d in deltas
    ]
    parts += [
        _event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 12}}),
        _event("message_stop", {"type": "message_stop"}),
    ]
    return "".join(parts)


class Upstream:
    def __init__(self, body: str = "", chunk_size: int = 7) -> None:
        self.body = body
        self.chunk_size = chunk_size
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = self.body.encode()
        size = self.chunk_size

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(raw), size):
                yield raw[i : i + size]

        return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream"})

    @property
    def last_payload(self) -> Any:
        return json.loads(self.requests[-1].content)


async def _proxy(upstream: Upstream, **kwargs: Any) -> tuple[HTTPProxy, Any]:
    from privyx.providers.generic import GenericProvider

    settings = load_config()
    engine, close = await build_engine(settings)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    provider = GenericProvider(base_url=URL, client=client, **kwargs)
    proxy = HTTPProxy(
        engine=engine, provider=provider, stream_adapter=build_stream_adapter("anthropic")
    )
    return proxy, close


def _events(raw: str) -> list[SSEEvent]:
    decoder = SSEDecoder()
    return decoder.feed(raw) + decoder.flush()


def _text(raw: str) -> str:
    out = []
    for event in _events(raw):
        payload = json.loads(event.data)
        if payload.get("type") == "content_block_delta":
            out.append(payload["delta"]["text"])
    return "".join(out)


# --------------------------------------------------------------------------


async def test_stream_round_trip_and_event_order() -> None:
    """Text deltas are restored; every other event passes through in order."""
    upstream = Upstream()
    proxy, close = await _proxy(upstream, headers={"anthropic-version": "2023-06-01"})
    try:
        payload, session_id = await proxy.process_request(
            {
                "model": "claude-sonnet-4",
                "max_tokens": 256,
                "system": f"The operator is {EMAIL}",
                "messages": [{"role": "user", "content": f"reach me at {PHONE}"}],
            }
        )
        session = await proxy._engine.vault.get(session_id)  # noqa: SLF001
        assert session is not None
        email_pseudonym = session.pseudonym_for(EMAIL)
        phone_pseudonym = session.pseudonym_for(PHONE)
        assert email_pseudonym and phone_pseudonym

        upstream.body = anthropic_stream(
            ["I will mail ", email_pseudonym, " and call ", phone_pseudonym, "."]
        )
        out = "".join([c async for c in proxy.process_stream(payload, session_id)])

        sent = json.dumps(upstream.last_payload)
        assert EMAIL not in sent and PHONE not in sent
        assert upstream.requests[-1].headers["anthropic-version"] == "2023-06-01"

        assert "<PRIVYX_" not in out
        assert _text(out) == f"I will mail {EMAIL} and call {PHONE}."

        types = [json.loads(ev.data)["type"] for ev in _events(out)]
        assert types[0] == "message_start"
        assert types[1] == "content_block_start"
        assert types[-1] == "message_stop"
        assert "message_delta" in types
        # Named events keep their `event:` line through the rewrite.
        assert all(ev.event == json.loads(ev.data)["type"] for ev in _events(out))
    finally:
        await close()
        await proxy._provider.close()  # noqa: SLF001


async def test_non_text_deltas_are_not_rewritten() -> None:
    """Thinking deltas and other non-text blocks pass through byte-for-byte."""
    upstream = Upstream()
    thinking = _event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm <PRIVYX_EMAIL_1>"},
        },
    )
    proxy, close = await _proxy(upstream)
    try:
        session = await proxy._engine.get_or_create_session()  # noqa: SLF001
        upstream.body = thinking + _event("message_stop", {"type": "message_stop"})

        out = "".join([c async for c in proxy.process_stream({}, session.session_id)])

        assert "thinking_delta" in out
        assert "hmm <PRIVYX_EMAIL_1>" in out  # unmapped, so untouched
    finally:
        await close()
        await proxy._provider.close()  # noqa: SLF001


async def test_pseudonym_split_across_events_and_chunks() -> None:
    """A pseudonym split across both events *and* byte chunks still restores."""
    upstream = Upstream(chunk_size=3)
    proxy, close = await _proxy(upstream)
    try:
        payload, session_id = await proxy.process_request(
            {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
        )
        session = await proxy._engine.vault.get(session_id)  # noqa: SLF001
        assert session is not None
        pseudonym = session.pseudonym_for(EMAIL)
        assert pseudonym is not None

        # One character per event, and 3 bytes per network chunk.
        upstream.body = anthropic_stream(["to ", *pseudonym, " ok"])
        out = "".join([c async for c in proxy.process_stream(payload, session_id)])

        assert _text(out) == f"to {EMAIL} ok"
    finally:
        await close()
        await proxy._provider.close()  # noqa: SLF001
