"""Google Gemini provider for gateway mode.

Gemini names the model and the method in the URL
(``…/v1beta/models/gemini-2.5-flash:streamGenerateContent``), not in the body,
so one fixed endpoint cannot serve it.  :class:`GoogleProvider` posts to the
path the client called: under ``base_url`` as it is when that is an origin,
and with the client's API version replaced by ``base_url``'s path otherwise
(Vertex AI's ``…/v1/projects/P/locations/L/publishers/google``).  The bodies
are masked and restored by the ``gemini`` wire schema, not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from privyx.providers._generic import GenericProvider
from privyx.providers._urls import DEFAULT_BASE_URLS

if TYPE_CHECKING:
    import httpx

#: The method whose reply streams; ``alt=sse`` asks for it as SSE.
_STREAM = ":streamGenerateContent"


class GoogleProvider(GenericProvider):
    """Gemini API transport: the key goes in ``x-goog-api-key``.

    ``base_url`` is the API origin, such as
    ``https://generativelanguage.googleapis.com``, or a root with a path that
    takes the place of the client's API version.  A full endpoint is cut back
    to the part before ``/models/``.
    """

    name = "google"
    default_url: ClassVar[str] = DEFAULT_BASE_URLS["google"]

    def url(self, path: str, *, stream: bool) -> str:
        """The client's ``path`` under ``base_url``'s root; a stream is asked for as SSE.

        Under a root with a path of its own, ``path`` loses its first segment,
        the API version: ``/v1beta/models/m:generateContent`` under
        ``…/publishers/google`` is ``…/publishers/google/models/m:generateContent``.
        ``alt=sse`` asks for a stream as SSE, what the router reads.
        """
        base = self.base_url
        if not path:
            return base
        cut = base.find("/models/")
        root = base[:cut] if cut >= 0 else base
        path = "/" + path.lstrip("/")
        if urlsplit(root).path.strip("/"):
            path = "/" + path.lstrip("/").partition("/")[2]
        url = root + path
        return f"{url}?alt=sse" if stream and url.endswith(_STREAM) else url

    def auth(self) -> httpx.Auth | None:
        return self._key("x-goog-api-key")
