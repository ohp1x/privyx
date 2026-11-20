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

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from privyx.core import ProviderError, Session
from privyx.engine import PrivacyEngine
from privyx.providers import Provider
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
        provider: Where a request goes, and its credential.
        client: The client requests are sent with (owned by the caller).
        stream_adapter: SSE stream adapter for extracting/rewriting deltas.
        schema: The wire schema of the requests this proxy serves.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        provider: Provider,
        client: httpx.AsyncClient,
        stream_adapter: SSEStreamAdapter | None = None,
        schema: str = "openai",
    ) -> None:
        self._engine = engine
        self._schema = schema
        self._provider = provider
        self._client = client
        self._adapter = stream_adapter or SSEStreamAdapter()

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
        :meth:`~privyx.engine.PrivacyEngine.ephemeral_session`, is masked
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
        """Forward a batch (non-streaming) request to the upstream provider.

        Raises:
            ProviderError: If the upstream cannot be reached or answers with an
                error, which it keeps.
        """
        try:
            response = await self._client.post(
                self._provider.url(path or "", stream=False),
                json=payload,
                headers=self._provider.headers(),
                auth=self._provider.auth() or httpx.USE_CLIENT_DEFAULT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise _provider_error("upstream request failed", exc) from exc
        try:
            return response.json()
        except ValueError:
            return response.text

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

        async for raw_chunk in self._upstream_stream(payload, path or ""):
            for out in router.feed(raw_chunk):
                yield out
        for out in router.flush():
            yield out

    async def _upstream_stream(self, payload: Any, path: str) -> AsyncGenerator[str, None]:
        """Yield raw upstream text with its SSE framing intact.

        Deliberately *not* ``aiter_lines()``: that strips the newlines that
        delimit SSE events, so a downstream parser could never tell where one
        event ends.

        Raises:
            ProviderError: If the upstream cannot be reached or answers with an
                error, which it keeps.
        """
        try:
            async with self._client.stream(
                "POST",
                self._provider.url(path, stream=True),
                json=payload,
                headers={**self._provider.headers(), "Accept": "text/event-stream"},
                auth=self._provider.auth() or httpx.USE_CLIENT_DEFAULT,
            ) as response:
                if response.is_error:
                    await response.aread()  # keep the error body for the caller
                response.raise_for_status()
                async for chunk in response.aiter_text():
                    yield chunk
        except httpx.HTTPError as exc:
            raise _provider_error("upstream stream failed", exc) from exc


#: Seconds to wait before each retry of a connection that could not be opened.
_CONNECT_RETRY_DELAYS = (0.5, 1.0)


class _UpstreamClient(httpx.AsyncClient):
    """Retries a request whose connection could not be opened.

    Only :class:`httpx.ConnectError` (refused, DNS, TLS handshake): nothing has
    been sent, so even a billed POST is safe to repeat.  A connect timeout is not
    retried, since every attempt would wait ``connect_timeout`` again.  Done here
    rather than with ``AsyncHTTPTransport(retries=...)``: a client given its own
    transport ignores ``HTTPS_PROXY`` and ``NO_PROXY``.
    """

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        for delay in _CONNECT_RETRY_DELAYS:
            try:
                return await super().send(request, **kwargs)
            except httpx.ConnectError:
                await asyncio.sleep(delay)
        return await super().send(request, **kwargs)


def upstream_client(
    *, timeout: float = 300.0, connect_timeout: float = 10.0, max_connections: int | None = None
) -> httpx.AsyncClient:
    """The httpx client both proxy modes reach the upstream with.

    ``timeout`` bounds each read and write, not the whole exchange: an upstream
    can take minutes to send its first byte, and a stream lasts as long as bytes
    keep coming.  ``max_connections`` of ``None`` sets no limit, so the proxy is
    never narrower than its clients; httpx's default of 100 made request 101
    onward wait for a free connection and fail.  A connection that cannot be
    opened is retried twice; see :class:`_UpstreamClient`.
    """
    return _UpstreamClient(
        timeout=httpx.Timeout(connect=connect_timeout, read=timeout, write=timeout, pool=10.0),
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=20),
    )


def _provider_error(prefix: str, exc: httpx.HTTPError) -> ProviderError:
    """Wrap ``exc``, keeping the upstream's error response when there is one."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return ProviderError(f"{prefix}: {exc}")
    response = exc.response
    return ProviderError(
        f"{prefix}: {exc}",
        status_code=response.status_code,
        body=response.content,
        content_type=response.headers.get("content-type", ""),
    )
