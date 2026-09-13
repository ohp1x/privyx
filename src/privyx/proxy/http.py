"""HTTP proxy — intercepts chat-completion requests and streams.

The proxy is a thin ASGI/WSGI wrapper that connects the privacy engine to
the upstream provider.  It handles:
  1. Decode incoming request (JSON payload).
  2. Pseudonymize sensitive content.
  3. Forward to the provider.
  4. Deanonymize the response (batch or streaming).
  5. Return the deanonymized response.
"""

from __future__ import annotations

import codecs
import copy
from collections.abc import AsyncGenerator
from typing import Any

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import SessionNotFoundError
from privyx.providers.base import Provider
from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.deanonymizer import StreamingDeanonymizer
from privyx.streaming.sse import SSEDecoder, SSEEvent


class HTTPProxy:
    """Privacy proxy for a single provider endpoint.

    Args:
        engine: Configured PrivacyEngine instance.
        provider: Upstream HTTP provider (any Provider implementation).
        stream_adapter: SSE stream adapter for extracting/rewriting deltas.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        provider: Provider,
        stream_adapter: SSEStreamAdapter | None = None,
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._adapter = stream_adapter or SSEStreamAdapter()

    async def process_request(
        self,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Pseudonymize the request payload and return (transformed, session_id)."""
        session = await self._engine.get_or_create_session(session_id)
        transformed = await self._transform_payload(payload, session.session_id)
        return transformed, session.session_id

    async def send_batch(self, payload: dict[str, Any]) -> Any:
        """Forward a batch (non-streaming) request to the upstream provider."""
        return await self._provider.send(payload)

    async def _transform_payload(
        self,
        payload: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        """Apply privacy transforms to known text fields in the payload."""
        result = copy.deepcopy(payload)

        # Transform messages content.
        if "messages" in result and isinstance(result["messages"], list):
            for msg in result["messages"]:
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, str):
                    tr = await self._engine.transform(content, session_id=session_id)
                    msg["content"] = tr.text
                elif isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "text"
                            and isinstance(part.get("text"), str)
                        ):
                            tr = await self._engine.transform(part["text"], session_id=session_id)
                            part["text"] = tr.text
        # Transform the system prompt.
        if "system" in result and isinstance(result["system"], str):
            tr = await self._engine.transform(result["system"], session_id=session_id)
            result["system"] = tr.text
        return result

    async def process_response(
        self,
        response: Any,
        session_id: str,
    ) -> Any:
        """Deanonymize a batch response."""
        if isinstance(response, dict):
            return await self._deanonymize_response(response, session_id)
        return response

    async def _deanonymize_response(
        self,
        response: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        if "choices" in response and isinstance(response["choices"], list):
            for choice in response["choices"]:
                if isinstance(choice, dict):
                    msg = choice.get("message") or choice.get("delta", {})
                    if (
                        isinstance(msg, dict)
                        and isinstance(msg.get("content"), str)
                    ):
                        try:
                            tr = await self._engine.restore(msg["content"], session_id)
                            msg["content"] = tr.text
                        except SessionNotFoundError:
                            pass
        return response

    async def process_stream(
        self,
        payload: dict[str, Any],
        session_id: str,
    ) -> AsyncGenerator[Any, None]:
        """Forward a streaming request, deanonymizing each SSE chunk.

        The payload passed here is already pseudonymized (caller must have called
        :meth:`process_request` first). This method only handles the streaming
        direction: upstream → deanonymize → yield.

        Providers yield raw text with SSE framing intact; :class:`SSEDecoder`
        reassembles events across chunk boundaries, and
        :class:`StreamingDeanonymizer` restores pseudonyms across *delta*
        boundaries. Three buffers are in flight at once and none of their
        boundaries line up: a UTF-8 character may be split across byte chunks,
        an event across text chunks, and a pseudonym across events.

        An event whose delta is entirely held back is suppressed rather than
        forwarded: emitting the original would leak the pseudonym fragment that
        the deanonymizer is still waiting to complete.
        """
        deanonymizer = StreamingDeanonymizer()

        # Seed the deanonymizer with the current session mapping.
        session = await self._engine.vault.get(session_id)
        if session is not None:
            deanonymizer.update_mapping(session.mapping)

        decoder = SSEDecoder()
        # Last text-carrying event, reused as the envelope template when
        # flushing held-back text: re-wrapping into a bare SSEEvent would emit
        # plain text where the client expects the provider's JSON schema.
        template: SSEEvent | None = None

        def _flush_pending() -> str | None:
            """Drain held-back text so passthrough events keep their order."""
            remaining = deanonymizer.flush()
            return self._adapter.wrap_delta(remaining, template).to_str() if remaining else None

        def _handle(event: SSEEvent) -> list[str]:
            """Turn one upstream event into zero or more downstream events."""
            nonlocal template
            delta = self._adapter.extract_delta(event)
            if not delta:
                # Not a text delta (role/usage/thinking/[DONE]).  Emit any
                # held-back text first so ordering is preserved, then pass the
                # event through untouched.
                pending = _flush_pending()
                return [pending, event.to_str()] if pending else [event.to_str()]

            template = event
            deanonymized = deanonymizer.feed(delta)
            if not deanonymized:
                # Fully held back — suppress until the pseudonym completes.
                return []
            return [self._adapter.wrap_delta(deanonymized, event).to_str()]

        # Incremental UTF-8 decoder: a provider yielding bytes may split a
        # multi-byte character across chunks, and decoding each chunk on its
        # own would turn it into replacement characters.  This holds the
        # partial sequence until the rest arrives.
        utf8 = codecs.getincrementaldecoder("utf-8")("replace")

        async for raw_chunk in self._provider.stream(payload):
            # Separate SSE envelope parsing from text transformation.
            # SSE envelope ≠ text stream (principle #8).
            chunk = utf8.decode(raw_chunk) if isinstance(raw_chunk, bytes) else raw_chunk
            for event in decoder.feed(chunk):
                for out in _handle(event):
                    yield out

        # Emit any event left unterminated by the upstream, then flush the
        # pseudonym fragment held back at the stream boundary.
        for event in decoder.feed(utf8.decode(b"", final=True)) + decoder.flush():
            for out in _handle(event):
                yield out

        remaining = _flush_pending()
        if remaining is not None:
            yield remaining