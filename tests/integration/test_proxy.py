"""Integration tests through :class:`HTTPProxy` — the transport seam.

These tests exist because of a real regression: the proxy fed newline-stripped
provider lines to a *stateless* SSE parser, which never saw the ``\\n\\n``
terminator and so emitted zero events.  Every chunk fell through to the
passthrough branch and pseudonyms reached the client verbatim.  Nothing caught
it, because the streaming tests hand-assembled :class:`SSEEvent` objects and
never crossed the provider → proxy boundary.

So: drive everything through ``HTTPProxy.process_stream`` with a fake provider
whose framing we control, and assert on the bytes a client would actually see.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterable
from typing import Any

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.providers.base import BaseProvider
from privyx.proxy.http import HTTPProxy
from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
from privyx.streaming.adapters.openai import OpenAIStreamAdapter
from privyx.streaming.sse import SSEDecoder
from privyx.vault.memory import MemoryVault

EMAIL = "alice@example.com"
PHONE = "+1-555-0142"


class FakeProvider(BaseProvider):
    """Provider that replays a fixed list of raw chunks.

    The chunks are raw text with SSE framing intact — exactly what a real
    transport hands over — so the proxy's decoder is genuinely exercised.
    """

    def __init__(self, chunks: Iterable[str], batch: Any = None) -> None:
        self.chunks = list(chunks)
        self.batch = batch
        self.sent: list[Any] = []
        self.closed = False

    async def send(self, payload: Any, session_id: str | None = None) -> Any:
        self.sent.append(payload)
        return self.batch

    async def stream(  # type: ignore[override]
        self, payload: Any, session_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        self.sent.append(payload)
        for chunk in self.chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


def _engine() -> PrivacyEngine:
    return PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )


def _openai_event(text: str) -> str:
    payload = {"choices": [{"delta": {"content": text}, "index": 0}]}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _openai_sse(deltas: Iterable[str]) -> str:
    """Render a full OpenAI-style SSE response body."""
    return "".join(_openai_event(d) for d in deltas) + "data: [DONE]\n\n"


def _anthropic_sse(deltas: Iterable[str]) -> str:
    parts = ['event: message_start\ndata: {"type":"message_start"}\n\n']
    for delta in deltas:
        payload = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": delta},
        }
        parts.append(
            f"event: content_block_delta\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        )
    parts.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    return "".join(parts)


def _collect_text(raw: str, adapter: Any) -> str:
    """Re-decode a proxy's output and concatenate its text deltas."""
    decoder = SSEDecoder()
    events = decoder.feed(raw) + decoder.flush()
    return "".join(adapter.extract_delta(ev) for ev in events)


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _run_stream(proxy: HTTPProxy, payload: dict[str, Any], session_id: str) -> str:
    return "".join([chunk async for chunk in proxy.process_stream(payload, session_id)])


# --------------------------------------------------------------------------
# Streaming


@pytest.mark.parametrize(
    "chunk_size",
    [None, 1, 3, 17, 64],
    ids=["whole-body", "char-by-char", "tiny", "mid-event", "large"],
)
async def test_stream_restores_pseudonym_at_any_framing(chunk_size: int | None) -> None:
    """No matter how the upstream frames its bytes, the client sees the original.

    ``None`` means one chunk for the whole body; the integer sizes slice the
    body blind, so events and pseudonyms are split at arbitrary offsets.
    """
    engine = _engine()
    proxy_payload, session_id = await HTTPProxy(
        engine=engine, provider=FakeProvider([]), stream_adapter=OpenAIStreamAdapter()
    ).process_request(
        {"messages": [{"role": "user", "content": f"Email {EMAIL} please"}]}
    )

    session = await engine.vault.get(session_id)
    assert session is not None
    (pseudonym,) = session.mapping.keys()
    assert session.mapping[pseudonym] == EMAIL

    body = _openai_sse(["Sure, ", "writing to ", pseudonym, " now."])
    chunks = [body] if chunk_size is None else _split(body, chunk_size)
    provider = FakeProvider(chunks)
    proxy = HTTPProxy(engine=engine, provider=provider, stream_adapter=OpenAIStreamAdapter())

    out = await _run_stream(proxy, proxy_payload, session_id)

    assert pseudonym not in out, "pseudonym leaked to the client"
    text = _collect_text(out, OpenAIStreamAdapter())
    assert text == f"Sure, writing to {EMAIL} now."
    assert "data: [DONE]" in out


async def test_stream_pseudonym_split_across_events() -> None:
    """A pseudonym straddling several SSE events still round-trips.

    The deanonymizer's hold-back buffer and the decoder's event buffer are
    independent; this is the case where both are mid-flight at once.
    """
    engine = _engine()
    proxy_payload, session_id = await HTTPProxy(
        engine=engine, provider=FakeProvider([]), stream_adapter=OpenAIStreamAdapter()
    ).process_request({"messages": [{"role": "user", "content": f"Call {PHONE}"}]})

    session = await engine.vault.get(session_id)
    assert session is not None
    (pseudonym,) = session.mapping.keys()

    # One character per event — the pathological case.
    deltas = ["Number: ", *pseudonym, " ok"]
    provider = FakeProvider([_openai_sse(deltas)])
    proxy = HTTPProxy(engine=engine, provider=provider, stream_adapter=OpenAIStreamAdapter())

    out = await _run_stream(proxy, proxy_payload, session_id)

    assert _collect_text(out, OpenAIStreamAdapter()) == f"Number: {PHONE} ok"


async def test_stream_flushes_held_text_before_done() -> None:
    """Text held back at the end of the stream is still delivered.

    A trailing partial pseudonym is buffered by the deanonymizer; if the proxy
    forgot to flush it, the client would silently lose the tail.
    """
    engine = _engine()
    session = await engine.get_or_create_session()
    # A prefix that looks like a pseudonym start but never completes.
    body = _openai_sse(["done <PRIVYX_EMA"])
    proxy = HTTPProxy(
        engine=engine, provider=FakeProvider([body]), stream_adapter=OpenAIStreamAdapter()
    )

    out = await _run_stream(proxy, {}, session.session_id)

    assert _collect_text(out, OpenAIStreamAdapter()) == "done <PRIVYX_EMA"


async def test_stream_preserves_non_text_events() -> None:
    """Role, usage and ``[DONE]`` events pass through untouched and in order."""
    engine = _engine()
    session = await engine.get_or_create_session()
    body = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        + _openai_event("hello")
        + 'data: {"usage":{"total_tokens":7}}\n\n'
        + "data: [DONE]\n\n"
    )
    proxy = HTTPProxy(
        engine=engine, provider=FakeProvider([body]), stream_adapter=OpenAIStreamAdapter()
    )

    out = await _run_stream(proxy, {}, session.session_id)

    decoder = SSEDecoder()
    events = decoder.feed(out) + decoder.flush()
    assert [ev.data for ev in events][0] == '{"choices":[{"delta":{"role":"assistant"}}]}'
    assert [ev.data for ev in events][-1] == "[DONE]"
    assert '"total_tokens":7' in out


async def test_stream_anthropic_events() -> None:
    """The Anthropic adapter round-trips through the same proxy path."""
    engine = _engine()
    proxy_payload, session_id = await HTTPProxy(
        engine=engine, provider=FakeProvider([]), stream_adapter=AnthropicStreamAdapter()
    ).process_request(
        {
            "system": f"Reply to {EMAIL}",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert EMAIL not in json.dumps(proxy_payload)

    session = await engine.vault.get(session_id)
    assert session is not None
    (pseudonym,) = session.mapping.keys()

    body = _anthropic_sse(["Sent to ", pseudonym[:6], pseudonym[6:], "."])
    provider = FakeProvider(_split(body, 11))
    proxy = HTTPProxy(engine=engine, provider=provider, stream_adapter=AnthropicStreamAdapter())

    out = await _run_stream(proxy, proxy_payload, session_id)

    assert pseudonym not in out
    assert _collect_text(out, AnthropicStreamAdapter()) == f"Sent to {EMAIL}."
    assert "message_stop" in out


async def test_stream_unterminated_final_event_is_emitted() -> None:
    """An upstream that closes without a trailing blank line loses nothing."""
    engine = _engine()
    session = await engine.get_or_create_session()
    body = _openai_event("hello ") + 'data: {"choices":[{"delta":{"content":"world"}}]}'
    proxy = HTTPProxy(
        engine=engine, provider=FakeProvider([body]), stream_adapter=OpenAIStreamAdapter()
    )

    out = await _run_stream(proxy, {}, session.session_id)

    assert _collect_text(out, OpenAIStreamAdapter()) == "hello world"


async def test_stream_with_unknown_session_passes_text_through() -> None:
    """An unknown session must not raise — it has no mapping, so text is verbatim."""
    engine = _engine()
    proxy = HTTPProxy(
        engine=engine,
        provider=FakeProvider([_openai_sse(["plain text"])]),
        stream_adapter=OpenAIStreamAdapter(),
    )

    out = await _run_stream(proxy, {}, "ses_does_not_exist")

    assert _collect_text(out, OpenAIStreamAdapter()) == "plain text"


async def test_stream_bytes_chunks_are_decoded() -> None:
    """Providers that yield bytes are handled the same as providers yielding str."""
    engine = _engine()
    session = await engine.get_or_create_session()
    body = _openai_sse(["héllo ", "wörld"])
    chunks: list[Any] = [c.encode() for c in _split(body, 9)]
    proxy = HTTPProxy(
        engine=engine, provider=FakeProvider(chunks), stream_adapter=OpenAIStreamAdapter()
    )

    out = await _run_stream(proxy, {}, session.session_id)

    assert _collect_text(out, OpenAIStreamAdapter()) == "héllo wörld"


@pytest.mark.parametrize("chunk_size", range(1, 12))
async def test_stream_multibyte_split_across_byte_chunks(chunk_size: int) -> None:
    """A UTF-8 character cut in half by a chunk boundary is not corrupted.

    Decoding each chunk on its own turns the orphaned bytes into U+FFFD, which
    is silent and irreversible — the client just receives mojibake. Slicing the
    body at every small size guarantees some cut lands mid-character.
    """
    engine = _engine()
    session = await engine.get_or_create_session()
    text = "héllo wörld — 日本語 🎉"
    payload = {"choices": [{"delta": {"content": text}}]}
    raw = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    chunks: list[Any] = [raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)]
    proxy = HTTPProxy(
        engine=engine, provider=FakeProvider(chunks), stream_adapter=OpenAIStreamAdapter()
    )

    out = await _run_stream(proxy, {}, session.session_id)

    assert _collect_text(out, OpenAIStreamAdapter()) == text
    assert "�" not in out


# --------------------------------------------------------------------------
# Request / batch response


async def test_process_request_pseudonymizes_all_text_fields() -> None:
    """Message strings, content parts and the system prompt are all covered."""
    engine = _engine()
    proxy = HTTPProxy(engine=engine, provider=FakeProvider([]))
    payload = {
        "system": f"Operator is {EMAIL}",
        "messages": [
            {"role": "user", "content": f"my phone is {PHONE}"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"and my mail is {EMAIL}"},
                    {"type": "image", "url": "https://example.com/x.png"},
                ],
            },
        ],
    }
    original = json.dumps(payload)

    transformed, session_id = await proxy.process_request(payload)

    serialized = json.dumps(transformed)
    assert EMAIL not in serialized
    assert PHONE not in serialized
    assert "<PRIVYX_" in serialized
    # The image part is untouched, and the caller's payload is not mutated.
    assert transformed["messages"][1]["content"][1]["url"] == "https://example.com/x.png"
    assert json.dumps(payload) == original

    # The same value gets the same pseudonym in both places it appears.
    session = await engine.vault.get(session_id)
    assert session is not None
    email_pseudonym = session.pseudonym_for(EMAIL)
    assert serialized.count(f'"{email_pseudonym}') or email_pseudonym in serialized
    assert transformed["system"] == f"Operator is {email_pseudonym}"


async def test_process_response_restores_batch_content() -> None:
    """A non-streaming response is deanonymized in place."""
    engine = _engine()
    proxy = HTTPProxy(engine=engine, provider=FakeProvider([]))
    transformed, session_id = await proxy.process_request(
        {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    )
    session = await engine.vault.get(session_id)
    assert session is not None
    pseudonym = session.pseudonym_for(EMAIL)

    response = {"choices": [{"message": {"role": "assistant", "content": f"ok, {pseudonym}"}}]}
    restored = await proxy.process_response(response, session_id)

    assert restored["choices"][0]["message"]["content"] == f"ok, {EMAIL}"


async def test_process_response_unknown_session_is_not_an_error() -> None:
    """A missing session leaves the response untouched rather than raising."""
    proxy = HTTPProxy(engine=_engine(), provider=FakeProvider([]))
    response = {"choices": [{"message": {"content": "<PRIVYX_EMAIL_1>"}}]}

    restored = await proxy.process_response(response, "ses_missing")

    assert restored["choices"][0]["message"]["content"] == "<PRIVYX_EMAIL_1>"


async def test_send_batch_forwards_payload() -> None:
    provider = FakeProvider([], batch={"ok": True})
    proxy = HTTPProxy(engine=_engine(), provider=provider)

    result = await proxy.send_batch({"model": "x"})

    assert result == {"ok": True}
    assert provider.sent == [{"model": "x"}]
