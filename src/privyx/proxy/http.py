"""HTTP proxy — intercepts chat-completion requests and streams.

The proxy is a thin ASGI/WSGI wrapper that connects the privacy engine to
the upstream provider.  It handles:
  1. Decode incoming request (JSON payload).
  2. Pseudonymize sensitive content.
  3. Forward to the provider.
  4. Deanonymize the response (batch or streaming).
  5. Return the deanonymized response.

The request pseudonymization and batch restore live in
:mod:`privyx.proxy.schemas`, and the streaming transform in
:class:`~privyx.proxy.stream_router.StreamRouter`, so this class and the
transparent proxy share exactly one implementation of each.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from privyx.core.engine import PrivacyEngine
from privyx.providers.base import Provider
from privyx.proxy.schemas import restore_response, transform_request
from privyx.proxy.stream_router import StreamRouter, resolver_for, select_processor_factory
from privyx.streaming.adapters.generic import SSEStreamAdapter


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
        transformed = await transform_request(payload, self._engine, session.session_id)
        return transformed, session.session_id

    async def send_batch(self, payload: dict[str, Any]) -> Any:
        """Forward a batch (non-streaming) request to the upstream provider."""
        return await self._provider.send(payload)

    async def process_response(
        self,
        response: Any,
        session_id: str,
    ) -> Any:
        """Deanonymize a batch response."""
        if isinstance(response, dict):
            return await restore_response(
                self._adapter.schema_name, response, self._engine, session_id
            )
        return response

    async def process_stream(
        self,
        payload: dict[str, Any],
        session_id: str,
    ) -> AsyncGenerator[Any, None]:
        """Forward a streaming request, deanonymizing each SSE chunk.

        The payload passed here is already pseudonymized (caller must have called
        :meth:`process_request` first). This method only handles the streaming
        direction: upstream → deanonymize → yield.  All SSE reassembly, held-back
        text ordering, and tool-call buffering live in :class:`StreamRouter`.
        """
        session = await self._engine.vault.get(session_id)
        mapping = session.mapping if session is not None else {}
        resolve = resolver_for(self._engine.operator, mapping)
        router = StreamRouter(
            self._engine.codec,
            resolve,
            self._adapter,
            make_processor=select_processor_factory(
                self._engine.operator, self._engine.codec, resolve, mapping
            ),
        )

        async for raw_chunk in self._provider.stream(payload):
            for out in router.feed(raw_chunk):
                yield out
        for out in router.flush():
            yield out
