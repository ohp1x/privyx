"""Integration tests: Anthropic-shaped traffic through the whole stack.

Anthropic's stream differs from OpenAI's in ways the proxy has to respect: the
text lives in ``content_block_delta`` events, named events carry an ``event:``
line that must survive rewriting, and several event types in the same stream
are not text at all.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import httpx
import pytest

from privyx import load_config
from privyx.app._build import build_engine
from privyx.core import EventBus, Exchange
from privyx.events import RequestMasked
from privyx.proxy import Pipeline
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
        self.content_type = "text/event-stream"
        self.read_to_end = False
        self.fail_read = False
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = self.body.encode()
        size = self.chunk_size

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(0, len(raw), size):
                yield raw[i : i + size]
            self.read_to_end = True
            if self.fail_read:
                raise httpx.ReadError("upstream disconnected")

        return httpx.Response(200, content=chunks(), headers={"content-type": self.content_type})

    @property
    def last_payload(self) -> Any:
        return json.loads(self.requests[-1].content)


#: The session every request of a test is masked in.
SESSION = "ses_anthropic"


async def _proxy(
    upstream: Upstream,
    *,
    terms: dict[str, list[str]] | None = None,
    events: EventBus | None = None,
    **kwargs: Any,
) -> tuple[Pipeline, Any]:
    from privyx.providers import GenericProvider

    settings = load_config(extra={"detectors": {"regex": {"terms": terms}}} if terms else None)
    engine, close = await build_engine(settings)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    provider = GenericProvider(base_url=URL, **kwargs)
    proxy = Pipeline(engine, provider, gateway=True, client=client, events=events)

    async def close_all() -> None:
        await close()
        await client.aclose()

    return proxy, close_all


def _exchange(payload: dict[str, Any]) -> Exchange:
    headers = {"x-privyx-session": SESSION}
    body = json.dumps(payload).encode()
    return Exchange("POST", "/v1/messages", headers, body)


async def _stream(proxy: Pipeline, payload: dict[str, Any]) -> str:
    """What the client gets for ``payload``, posted to ``/v1/messages`` in the session."""
    reply = await proxy.handle(_exchange(payload))
    assert reply.stream is not None
    return "".join([c if isinstance(c, str) else c.decode() async for c in reply.stream])


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
        payload = {
            "model": "claude-sonnet-4",
            "max_tokens": 256,
            "system": f"The operator is {EMAIL}",
            "messages": [{"role": "user", "content": f"reach me at {PHONE}"}],
        }
        await _stream(proxy, payload)  # masked once, so the pseudonyms are known
        session = await proxy.engine.vault.get(SESSION)
        assert session is not None
        email_pseudonym = session.pseudonym_for(EMAIL)
        phone_pseudonym = session.pseudonym_for(PHONE)
        assert email_pseudonym and phone_pseudonym

        upstream.body = anthropic_stream(
            ["I will mail ", email_pseudonym, " and call ", phone_pseudonym, "."]
        )
        out = await _stream(proxy, {**payload, "stream": True})

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
        upstream.body = thinking + _event("message_stop", {"type": "message_stop"})

        out = await _stream(proxy, {"stream": True})

        assert "thinking_delta" in out
        assert "hmm <PRIVYX_EMAIL_1>" in out  # unmapped, so untouched
    finally:
        await close()


async def test_pseudonym_split_across_events_and_chunks() -> None:
    """A pseudonym split across both events *and* byte chunks still restores."""
    upstream = Upstream(chunk_size=3)
    proxy, close = await _proxy(upstream)
    try:
        await _stream(proxy, {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]})
        session = await proxy.engine.vault.get(SESSION)
        assert session is not None
        pseudonym = session.pseudonym_for(EMAIL)
        assert pseudonym is not None

        # One character per event, and 3 bytes per network chunk.
        upstream.body = anthropic_stream(["to ", *pseudonym, " ok"])
        out = await _stream(proxy, {"stream": True})

        assert _text(out) == f"to {EMAIL} ok"
    finally:
        await close()


async def _structured_reply(
    proxy: Pipeline, upstream: Upstream, payload: dict[str, Any], mode: str, text: str
) -> str:
    if mode == "stream":
        upstream.body = anthropic_stream(list(text))
        return _text(await _stream(proxy, payload))
    upstream.body = json.dumps({"content": [{"type": "text", "text": text}]})
    if mode == "batch":
        upstream.content_type = "application/json"
        reply = await proxy.handle(_exchange(payload))
        assert reply.body is not None
        response = json.loads(reply.body)
    else:
        response = json.loads(await _stream(proxy, payload))
    return response["content"][0]["text"]


@pytest.mark.parametrize("mode", ["stream", "batch", "batch_as_sse"])
@pytest.mark.parametrize("via_hook", [False, True])
async def test_structured_title_restores_escaped_pseudonyms(mode: str, via_hook: bool) -> None:
    organization = 'Acme "Widget"'
    first = {"role": "user", "content": f"Plan the {organization} release"}
    output_config = {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    payload: dict[str, Any] = {"messages": [first], "stream": mode == "stream"}
    bus = EventBus()

    def request_json(event: RequestMasked) -> None:
        event.body["output_config"] = output_config

    if via_hook:
        bus.add(RequestMasked, request_json)
    else:
        payload["output_config"] = output_config

    upstream = Upstream(chunk_size=3)
    proxy, close = await _proxy(upstream, terms={"ORGANIZATION": [organization]}, events=bus)
    try:
        await _stream(proxy, payload)
        session = await proxy.engine.vault.get(SESSION)
        assert session is not None
        pseudonym = session.pseudonym_for(organization)
        assert pseudonym is not None
        assert upstream.last_payload["output_config"] == output_config

        escaped = json.dumps(pseudonym).replace("<", r"\u003c").replace(">", r"\u003e")
        document = (
            r'{ "title": %s, %s: "\u00e9\t", "large": 1e400, "precise": 0.1234567890123456789 }'
        )
        title = document % (escaped, escaped)
        restored = await _structured_reply(proxy, upstream, payload, mode, title)

        assert restored == document % (json.dumps(organization), json.dumps(organization))

        # The client replays the restored assistant message in an ordinary request.
        if via_hook:
            bus.remove(request_json)
        if mode == "stream":
            content: Any = [
                {"type": "text", "text": restored},
                {"type": "text", "text": '{ "title": ' + json.dumps(organization)[:-1]},
                {"type": "text", "text": '{ "title": ' + json.dumps(organization)[:-1] + r"\u02"},
            ]
        else:
            content = restored
        history = [first, {"role": "assistant", "content": content}]
        reply = await proxy.handle(_exchange({"messages": history}))
        if reply.stream is not None:
            await reply.stream.aclose()
        sent_payload = upstream.last_payload
        assert "output_config" not in sent_payload
        sent = sent_payload["messages"][1]["content"]
        masked = document % (json.dumps(pseudonym), json.dumps(pseudonym))
        if isinstance(sent, list):
            partial = '{ "title": ' + json.dumps(pseudonym)[:-1]
            assert [block["text"] for block in sent] == [masked, partial, partial + r"\u02"]
        else:
            assert sent == masked
    finally:
        await close()


@pytest.mark.parametrize("ending", ["message_delta", "error", "eof", "transport"])
async def test_structured_stream_drains_before_terminal_event_or_read_failure(
    ending: str,
) -> None:
    beginning = '{ "title": '
    payload = {
        "messages": [{"role": "user", "content": EMAIL}],
        "output_config": {"format": {"type": "json_schema"}},
        "stream": True,
    }
    upstream = Upstream()
    proxy, close = await _proxy(upstream)
    try:
        await _stream(proxy, payload)
        session = await proxy.engine.vault.get(SESSION)
        assert session is not None
        pseudonym = session.pseudonym_for(EMAIL)
        assert pseudonym is not None
        escaped = json.dumps(pseudonym).replace("<", r"\u003c").replace(">", r"\u003e")
        full = anthropic_stream([beginning, *escaped[:-1]])
        raw = full[: full.index("event: content_block_stop")]
        if ending in ("message_delta", "error"):
            raw += _event(ending, {"type": ending})
        upstream.body = raw
        upstream.read_to_end = False
        upstream.fail_read = ending == "transport"
        reply = await proxy.handle(_exchange(payload))
        assert reply.stream is not None
        chunks = []
        async with aclosing(reply.stream):
            async for chunk in reply.stream:
                chunks.append(chunk)
                if _text("".join(chunks)) == beginning:
                    break
            assert not upstream.read_to_end

            async def drain() -> None:
                async for chunk in reply.stream:
                    chunks.append(chunk)

            if upstream.fail_read:
                with pytest.raises(httpx.ReadError):
                    await drain()
            else:
                await drain()
        received = "".join(chunks)
        assert _text(received) == beginning + json.dumps(EMAIL)[:-1]
        if ending in ("message_delta", "error"):
            assert _events(received)[-1].event == ending
    finally:
        await close()


@pytest.mark.parametrize("mode", ["stream", "batch", "batch_as_sse"])
async def test_structured_replies_restore_prose_and_unquoted_tokens(mode: str) -> None:
    payload = {
        "messages": [{"role": "user", "content": EMAIL}],
        "output_config": {"format": {"type": "json_schema"}},
        "stream": mode == "stream",
    }
    upstream = Upstream(chunk_size=3)
    proxy, close = await _proxy(upstream)
    try:
        await _stream(proxy, payload)
        session = await proxy.engine.vault.get(SESSION)
        assert session is not None
        pseudonym = session.pseudonym_for(EMAIL)
        assert pseudonym is not None
        for template in ['Sorry, I cannot include "%s" in the title.', '{"title": %s}']:
            text = template % pseudonym
            restored = await _structured_reply(proxy, upstream, payload, mode, text)
            assert restored == template % EMAIL
    finally:
        await close()


async def test_masked_listener_can_answer_with_structured_text() -> None:
    bus = EventBus()
    organization = 'Acme "Widget"'

    def answer(event: RequestMasked) -> None:
        event.body["output_config"] = {"format": {"type": "json_schema"}}
        pseudonym = event.body["messages"][0]["content"]
        text = json.dumps({"title": pseudonym}).replace("<", r"\u003c").replace(">", r"\u003e")
        event.respond(200, {"content": [{"type": "text", "text": text}]})

    bus.add(RequestMasked, answer)
    upstream = Upstream()
    proxy, close = await _proxy(upstream, terms={"ORGANIZATION": [organization]}, events=bus)
    try:
        reply = await proxy.handle(
            _exchange({"messages": [{"role": "user", "content": organization}]})
        )
        assert reply.status_code == 200 and reply.body is not None
        assert upstream.requests == []
        text = json.loads(reply.body)["content"][0]["text"]
        assert json.loads(text) == {"title": organization}
    finally:
        await close()
