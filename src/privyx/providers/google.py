"""Google Gemini provider for gateway mode.

Gemini names the model and the method in the URL
(``…/v1beta/models/gemini-2.5-flash:streamGenerateContent``), not in the body,
so one fixed endpoint cannot serve it.  :class:`GoogleProvider` posts to the
path the client called: under ``base_url`` as it is when that is an origin,
and with the client's API version replaced by ``base_url``'s path otherwise
(Vertex AI's ``…/v1/projects/P/locations/L/publishers/google``).  The bodies
are the generic provider's: Gemini's JSON is masked and restored by the
``gemini`` wire schema, not here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from privyx.providers.generic import GenericProvider

#: The method whose reply streams; ``alt=sse`` asks for it as SSE.
_STREAM = ":streamGenerateContent"


class GoogleProvider(GenericProvider):
    """Gemini API transport: the key goes in ``x-goog-api-key``.

    Args:
        base_url: The API origin, such as
            ``https://generativelanguage.googleapis.com``, or a root with a
            path that takes the place of the client's API version.  A full
            endpoint is cut back to the part before ``/models/``.
        api_key: Sent as ``x-goog-api-key`` unless ``headers`` has one.
        headers, client, timeout, connect_timeout, max_connections: As for
            :class:`~privyx.providers.generic.GenericProvider`.
    """

    name = "google"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        **options: Any,
    ) -> None:
        headers = dict(headers or {})
        if api_key and not any(k.lower() == "x-goog-api-key" for k in headers):
            headers["x-goog-api-key"] = api_key
        super().__init__(base_url, headers=headers, client=client, **options)
        cut = self._base_url.find("/models/")
        self._root = self._base_url[:cut] if cut >= 0 else self._base_url

    def _resolve_url(self, path: str | None) -> str:
        """The client's ``path`` under ``base_url``'s root.

        Under a root with a path of its own, ``path`` loses its first segment,
        the API version: ``/v1beta/models/m:generateContent`` under
        ``…/publishers/google`` is ``…/publishers/google/models/m:generateContent``.
        """
        if not path:
            return self._base_url
        path = "/" + path.lstrip("/")
        if urlsplit(self._root).path.strip("/"):
            path = "/" + path.lstrip("/").partition("/")[2]
        return self._root + path

    def _stream_url(self, path: str | None) -> str:
        """A stream is asked for as SSE (``alt=sse``), what the router reads."""
        url = self._resolve_url(path)
        return f"{url}?alt=sse" if url.endswith(_STREAM) else url
