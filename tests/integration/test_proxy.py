"""Integration tests through the gateway pipeline — the transport seam.

These tests exist because of a real regression: the proxy fed newline-stripped
provider lines to a *stateless* SSE parser, which never saw the ``\\n\\n``
terminator and so emitted zero events.  Every chunk fell through to the
passthrough branch and pseudonyms reached the client verbatim.  Nothing caught
it, because the streaming tests hand-assembled :class:`SSEEvent` objects and
never crossed the provider → proxy boundary.

So: drive everything through :meth:`Pipeline.handle` with a fake upstream
whose framing we control, and assert on the bytes a client would actually see.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx
import pytest

from privyx.core import Exchange
from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.operators._pseudonym import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.providers import GenericProvider
from privyx.proxy import Pipeline, Reply
from privyx.streaming import SSEStreamAdapter
from privyx.streaming.sse import SSEDecoder
from privyx.vaults import MemoryVault

EMAIL = "alice@example.com"
PHONE = "+1-555-0142"
CHAT = "/v1/chat/completions"


class FakeProvider(GenericProvider):
    """An upstream that replays a fixed list of raw chunks, through a client of its own.

    The chunks are raw text with SSE framing intact — exactly what a real
    transport hands over — so the proxy's decoder is genuinely exercised.
    """

    def __init__(self, chunks: Iterable[str], batch: Any = None, status: int = 200) -> None:
        super().__init__(base_url="https://upstream.example.com/v1/chat/completions")
        self.chunks = list(chunks)
        self.batch = batch
        self.status = status
        self.sent: list[Any] = []
        self.paths: list[str] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._reply))

    def _reply(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        self.paths.append(request.url.path)
        if "text/event-stream" in request.headers.get("accept", ""):
            sse = {"content-type": "text/event-stream"}
            return httpx.Response(self.status, headers=sse, stream=_Chunks(self.chunks))
        return httpx.Response(self.status, json=self.batch)


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[str | bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk if isinstance(chunk, bytes) else chunk.encode()


async def _send(
    engine: PrivacyEngine,
    provider: FakeProvider,
    payload: dict[str, Any],
    session_id: str = "ses_proxy",
    path: str = CHAT,
) -> Reply:
    """``payload`` through the gateway to ``provider``, in session ``session_id``."""
    pipeline = Pipeline(engine, provider, gateway=True, client=provider.client)
    headers = {"x-privyx-session": session_id}
    return await pipeline.handle(Exchange("POST", path, headers, json.dumps(payload).encode()))


async def _mask(
    engine: PrivacyEngine, payload: dict[str, Any], path: str = CHAT
) -> tuple[dict[str, Any], str]:
    """``payload`` as the upstream receives it, and the session it was masked in."""
    provider = FakeProvider([], batch={})
    reply = await _send(engine, provider, payload, path=path)
    assert reply.status_code == 200
    return provider.sent[-1], reply.headers["x-privyx-session"]


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


#: Where each stream keeps its text: OpenAI's Chat Completions, Anthropic's ``text_delta``.
OPENAI_TEXT = SSEStreamAdapter(data_path=["choices", "0", "delta", "content"])
ANTHROPIC_TEXT = SSEStreamAdapter(data_path=["delta", "text"])


def _collect_text(raw: str, adapter: Any) -> str:
    """Re-decode a proxy's output and concatenate its text deltas."""
    decoder = SSEDecoder()
    events = decoder.feed(raw) + decoder.flush()
    return "".join(adapter.extract_delta(ev) for ev in events)


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _run_stream(
    engine: PrivacyEngine, provider: FakeProvider, session_id: str, path: str = CHAT
) -> str:
    """The stream a client gets when ``provider`` replays its chunks in ``session_id``."""
    reply = await _send(engine, provider, {"stream": True}, session_id, path)
    assert reply.stream is not None
    return "".join([c if isinstance(c, str) else c.decode() async for c in reply.stream])


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
    _, session_id = await _mask(
        engine, {"messages": [{"role": "user", "content": f"Email {EMAIL} please"}]}
    )

    session = await engine.vault.get(session_id)
    assert session is not None
    (pseudonym,) = session.mapping.keys()
    assert session.mapping[pseudonym] == EMAIL

    body = _openai_sse(["Sure, ", "writing to ", pseudonym, " now."])
    chunks = [body] if chunk_size is None else _split(body, chunk_size)

    out = await _run_stream(engine, FakeProvider(chunks), session_id)

    assert pseudonym not in out, "pseudonym leaked to the client"
    text = _collect_text(out, OPENAI_TEXT)
    assert text == f"Sure, writing to {EMAIL} now."
    assert "data: [DONE]" in out


async def test_stream_pseudonym_split_across_events() -> None:
    """A pseudonym straddling several SSE events still round-trips.

    The deanonymizer's hold-back buffer and the decoder's event buffer are
    independent; this is the case where both are mid-flight at once.
    """
    engine = _engine()
    _, session_id = await _mask(
        engine, {"messages": [{"role": "user", "content": f"Call {PHONE}"}]}
    )

    session = await engine.vault.get(session_id)
    assert session is not None
    (pseudonym,) = session.mapping.keys()

    # One character per event — the pathological case.
    deltas = ["Number: ", *pseudonym, " ok"]

    out = await _run_stream(engine, FakeProvider([_openai_sse(deltas)]), session_id)

    assert _collect_text(out, OPENAI_TEXT) == f"Number: {PHONE} ok"


async def test_stream_flushes_held_text_before_done() -> None:
    """Text held back at the end of the stream is still delivered.

    A trailing partial pseudonym is buffered by the deanonymizer; if the proxy
    forgot to flush it, the client would silently lose the tail.
    """
    engine = _engine()
    session = await engine.get_or_create_session()
    # A prefix that looks like a pseudonym start but never completes.
    body = _openai_sse(["done <PRIVYX_EMA"])
    out = await _run_stream(engine, FakeProvider([body]), session.session_id)

    assert _collect_text(out, OPENAI_TEXT) == "done <PRIVYX_EMA"


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
    out = await _run_stream(engine, FakeProvider([body]), session.session_id)

    decoder = SSEDecoder()
    events = decoder.feed(out) + decoder.flush()
    assert [ev.data for ev in events][0] == '{"choices":[{"delta":{"role":"assistant"}}]}'
    assert [ev.data for ev in events][-1] == "[DONE]"
    assert '"total_tokens":7' in out


async def test_stream_anthropic_events() -> None:
    """The Anthropic adapter round-trips through the same proxy path."""
    engine = _engine()
    sent, session_id = await _mask(
        engine,
        {
            "system": f"Reply to {EMAIL}",
            "messages": [{"role": "user", "content": "hi"}],
        },
        "/v1/messages",
    )
    assert EMAIL not in json.dumps(sent)

    session = await engine.vault.get(session_id)
    assert session is not None
    (pseudonym,) = session.mapping.keys()

    body = _anthropic_sse(["Sent to ", pseudonym[:6], pseudonym[6:], "."])

    out = await _run_stream(engine, FakeProvider(_split(body, 11)), session_id, "/v1/messages")

    assert pseudonym not in out
    assert _collect_text(out, ANTHROPIC_TEXT) == f"Sent to {EMAIL}."
    assert "message_stop" in out


async def test_stream_unterminated_final_event_is_emitted() -> None:
    """An upstream that closes without a trailing blank line loses nothing."""
    engine = _engine()
    session = await engine.get_or_create_session()
    body = _openai_event("hello ") + 'data: {"choices":[{"delta":{"content":"world"}}]}'
    out = await _run_stream(engine, FakeProvider([body]), session.session_id)

    assert _collect_text(out, OPENAI_TEXT) == "hello world"


async def test_stream_with_unknown_session_passes_text_through() -> None:
    """An unknown session must not raise — it has no mapping, so text is verbatim."""
    engine = _engine()
    provider = FakeProvider([_openai_sse(["plain text"])])

    out = await _run_stream(engine, provider, "ses_does_not_exist")

    assert _collect_text(out, OPENAI_TEXT) == "plain text"


async def test_stream_bytes_chunks_are_decoded() -> None:
    """Providers that yield bytes are handled the same as providers yielding str."""
    engine = _engine()
    session = await engine.get_or_create_session()
    body = _openai_sse(["héllo ", "wörld"])
    chunks: list[Any] = [c.encode() for c in _split(body, 9)]
    out = await _run_stream(engine, FakeProvider(chunks), session.session_id)

    assert _collect_text(out, OPENAI_TEXT) == "héllo wörld"


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
    out = await _run_stream(engine, FakeProvider(chunks), session.session_id)

    assert _collect_text(out, OPENAI_TEXT) == text
    assert "�" not in out


# --------------------------------------------------------------------------
# Request / batch response


async def test_a_request_is_pseudonymized_in_all_text_fields() -> None:
    """Message strings, content parts and the system prompt are all covered."""
    engine = _engine()
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

    transformed, session_id = await _mask(engine, payload)

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


async def test_a_batch_reply_is_restored() -> None:
    """A non-streaming response is deanonymized."""
    engine = _engine()
    _, session_id = await _mask(
        engine, {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]}
    )
    session = await engine.vault.get(session_id)
    assert session is not None
    pseudonym = session.pseudonym_for(EMAIL)

    response = {"choices": [{"message": {"role": "assistant", "content": f"ok, {pseudonym}"}}]}
    reply = await _send(engine, FakeProvider([], batch=response), {}, session_id)

    assert reply.body is not None
    assert json.loads(reply.body)["choices"][0]["message"]["content"] == f"ok, {EMAIL}"


async def test_a_reply_in_an_unknown_session_is_not_an_error() -> None:
    """A session with no mapping leaves the response untouched rather than raising."""
    response = {"choices": [{"message": {"content": "<PRIVYX_EMAIL_1>"}}]}

    reply = await _send(_engine(), FakeProvider([], batch=response), {}, "ses_missing")

    assert reply.body is not None
    assert json.loads(reply.body)["choices"][0]["message"]["content"] == "<PRIVYX_EMAIL_1>"


async def test_a_batch_request_is_forwarded() -> None:
    provider = FakeProvider([], batch={"ok": True})

    reply = await _send(_engine(), provider, {"model": "x"})

    assert reply.body is not None and json.loads(reply.body) == {"ok": True}
    assert provider.sent == [{"model": "x"}]


async def test_the_provider_decides_where_a_batch_goes() -> None:
    provider = FakeProvider([], batch={"ok": True})
    await _send(_engine(), provider, {}, path="/v1/embeddings")
    await _send(_engine(), provider, {}, path="/v1/chat/completions")

    assert provider.paths == ["/v1/embeddings", "/v1/chat/completions"]


async def test_an_upstream_error_keeps_its_status_and_body() -> None:
    provider = FakeProvider([], batch={"error": "quota"}, status=429)

    reply = await _send(_engine(), provider, {})

    assert reply.status_code == 429
    assert reply.body is not None and json.loads(reply.body) == {"error": "quota"}
