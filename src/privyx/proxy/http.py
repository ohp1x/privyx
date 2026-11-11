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

import inspect
from collections.abc import AsyncGenerator
from typing import Any

from privyx.core.engine import PrivacyEngine
from privyx.core.session import Session
from privyx.providers.base import Provider
from privyx.proxy.schemas import restore_response, transform_request
from privyx.proxy.stream_router import (
    StreamRouter,
    resolver_for,
    restore_chunks,
    select_processor_factory,
)
from privyx.streaming.adapters.generic import SSEStreamAdapter


class HTTPProxy:
    """Privacy proxy for a single provider endpoint.

    Args:
        engine: Configured PrivacyEngine instance.
        provider: Upstream HTTP provider (any Provider implementation).
        stream_adapter: SSE stream adapter for extracting/rewriting deltas.
        schema: The wire schema of the requests this proxy serves.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        provider: Provider,
        stream_adapter: SSEStreamAdapter | None = None,
        schema: str = "openai",
    ) -> None:
        self._engine = engine
        self._schema = schema
        self._provider = provider
        self._adapter = stream_adapter or SSEStreamAdapter()
        # The client's path, for a provider that can aim it (the generic one does);
        # a plugin's send(payload) keeps posting to its own endpoint.
        self._takes_path = "path" in inspect.signature(provider.send).parameters

    async def process_request(
        self,
        payload: dict[str, Any],
        session_id: str | None = None,
        *,
        source: str = "ephemeral",
        session: Session | None = None,
        path: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Pseudonymize the request payload and return (transformed, session_id).

        ``source`` records how ``session_id`` was chosen for the audit trail; see
        :func:`privyx.proxy.session.resolve_session_id`.  ``session``, an
        :meth:`~privyx.core.engine.PrivacyEngine.ephemeral_session`, is masked
        in memory instead of through the vault.  ``path`` is the one the client
        called (see :func:`~privyx.proxy.schemas.transform_request`).
        """
        if session is not None:
            transformed = await transform_request(
                payload,
                self._engine,
                session.session_id,
                session=session,
                schema=self._schema,
                path=path,
            )
            return transformed, session.session_id
        stored = await self._engine.get_or_create_session(session_id, source=source)
        transformed = await transform_request(
            payload, self._engine, stored.session_id, schema=self._schema, path=path
        )
        return transformed, stored.session_id

    async def send_batch(self, payload: dict[str, Any], path: str | None = None) -> Any:
        """Forward a batch (non-streaming) request to the upstream provider."""
        if self._takes_path:
            return await self._provider.send(payload, path=path)  # type: ignore[call-arg]
        return await self._provider.send(payload)

    async def process_response(
        self,
        response: Any,
        session_id: str,
        *,
        session: Session | None = None,
    ) -> Any:
        """Deanonymize a batch response; ``session``, when held, skips the vault read.

        A Gemini stream asked for without ``alt=sse`` comes back as a JSON
        array of chunks, restored in order like a stream (not counted in the
        audit trail, like a gateway stream).
        """
        if self._schema == "gemini" and isinstance(response, list):
            if session is None:
                session = await self._engine.vault.get(session_id)
            if session is None:
                return response
            restored, _ = restore_chunks(
                response, self._engine.operator, self._engine.codec, session.mapping
            )
            return restored
        if isinstance(response, dict):
            return await restore_response(
                response, self._engine, session_id, session=session, schema=self._schema
            )
        return response

    async def process_stream(
        self,
        payload: dict[str, Any],
        session_id: str,
        *,
        session: Session | None = None,
        path: str | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Forward a streaming request, deanonymizing each SSE chunk.

        The payload passed here is already pseudonymized (caller must have called
        :meth:`process_request` first). This method only handles the streaming
        direction: upstream → deanonymize → yield.  All SSE reassembly, held-back
        text ordering, and tool-call buffering live in :class:`StreamRouter`.
        ``session``, when held, skips the vault read.
        """
        if session is None:
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

        stream = (
            self._provider.stream(payload, path=path)  # type: ignore[call-arg]
            if self._takes_path
            else self._provider.stream(payload)
        )
        async for raw_chunk in stream:
            for out in router.feed(raw_chunk):
                yield out
        for out in router.flush():
            yield out
