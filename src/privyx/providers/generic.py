"""Generic HTTP provider — transparent passthrough for any endpoint.

Privyx does not need to know the endpoint schema: requests and responses are
relayed as-is, with the privacy engine applied to text leaves.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx

from privyx.core.errors import ProviderError
from privyx.providers.base import BaseProvider


class GenericProvider(BaseProvider):
    """HTTP passthrough provider.

    Args:
        base_url: Upstream base URL (e.g. ``http://localhost:20128``).
        api_key: Optional bearer token attached to upstream requests.
        headers: Extra headers merged into every request.
        client: Optional existing httpx.AsyncClient (owned by caller).
    """

    name = "generic"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))

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
            raise ProviderError(f"upstream request failed: {exc}") from exc
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
                response.raise_for_status()
                async for chunk in response.aiter_text():
                    yield chunk
        except httpx.HTTPError as exc:
            raise ProviderError(f"upstream stream failed: {exc}") from exc