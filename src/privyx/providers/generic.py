"""Generic HTTP provider — transparent passthrough for any endpoint.

Privyx does not need to know the endpoint schema: requests and responses are
relayed as-is, with the privacy engine applied to text leaves.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from privyx.core.errors import ProviderError
from privyx.providers.base import BaseProvider

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


class GenericProvider(BaseProvider):
    """HTTP passthrough provider.

    Args:
        base_url: Upstream base URL (e.g. ``http://localhost:20128``).
        api_key: Optional bearer token attached to upstream requests.
        headers: Extra headers merged into every request.
        client: Optional existing httpx.AsyncClient (owned by caller).
        timeout, connect_timeout, max_connections: For the client built when
            ``client`` is not given; see :func:`upstream_client`.
    """

    name = "generic"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 300.0,
        connect_timeout: float = 10.0,
        max_connections: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._owns_client = client is None
        self._client = client or upstream_client(
            timeout=timeout, connect_timeout=connect_timeout, max_connections=max_connections
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(self._headers)
        if extra:
            headers.update(extra)
        if self._api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def send(self, payload: Any, session_id: str | None = None) -> Any:
        url = self._base_url
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers=self._build_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise _provider_error("upstream request failed", exc) from exc
        try:
            return response.json()
        except ValueError:
            return response.text

    async def stream(
        self, payload: Any, session_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        """Yield raw upstream text with its SSE framing intact.

        Deliberately *not* ``aiter_lines()``: that strips the newlines that
        delimit SSE events, so a downstream parser could never tell where one
        event ends.  Callers reassemble events with
        :class:`~privyx.streaming.sse.SSEDecoder`.
        """
        url = self._base_url
        try:
            async with self._client.stream(
                "POST",
                url,
                json=payload,
                headers=self._build_headers({"Accept": "text/event-stream"}),
            ) as response:
                if response.is_error:
                    await response.aread()  # keep the error body for the caller
                response.raise_for_status()
                async for chunk in response.aiter_text():
                    yield chunk
        except httpx.HTTPError as exc:
            raise _provider_error("upstream stream failed", exc) from exc


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
