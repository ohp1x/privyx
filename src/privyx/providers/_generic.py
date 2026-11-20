"""The generic provider: a fixed upstream endpoint, with a bearer key."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit, urlunsplit

from privyx.providers._base import Provider
from privyx.providers._urls import DEFAULT_BASE_URLS

if TYPE_CHECKING:
    import httpx

#: The endpoints a base URL may end in.  ``chat/completions`` comes before
#: ``completions``, which it also ends in.
_ENDPOINTS = ("chat/completions", "completions", "embeddings", "messages", "responses")

#: Client paths that go to their own endpoint; every other path posts to the base URL.
_OWN_ENDPOINT = frozenset({"completions", "embeddings"})


class GenericProvider(Provider):
    """Posts every request to ``base_url``, with ``api_key`` as a bearer token.

    Embeddings and legacy completions go to their own endpoint beside it.
    """

    name = "generic"
    #: Where requests go when ``base_url`` is empty.
    default_url: ClassVar[str] = DEFAULT_BASE_URLS["generic"]

    class Options(Provider.Options):
        """A key per built-in type, which every built-in type takes.

        One environment can hold a key per type (``PRIVYX_OPENAI_API_KEY``,
        ...); the one for the selected type wins over ``api_key``.
        """

        openai_api_key: str = ""
        anthropic_api_key: str = ""
        google_api_key: str = ""

    options: Options

    @property
    def base_url(self) -> str:
        """``base_url``, or the type's default when it is empty, without a trailing ``/``."""
        return (self.options.base_url or self.default_url).rstrip("/")

    def url(self, path: str, *, stream: bool) -> str:
        """The base URL, or for embeddings and legacy completions their endpoint beside it.

        ``…/v1/chat/completions`` becomes ``…/v1/embeddings``; a base URL with no
        endpoint at its end (``…/v1``) gets the endpoint appended.
        """
        wanted = path.strip("/").removeprefix("v1/")
        if wanted not in _OWN_ENDPOINT:
            return self.base_url
        parts = urlsplit(self.base_url)
        prefix = parts.path.rstrip("/")
        for endpoint in _ENDPOINTS:
            if prefix.endswith(f"/{endpoint}"):
                prefix = prefix.removesuffix(f"/{endpoint}")
                break
        return urlunsplit(parts._replace(path=f"{prefix}/{wanted}"))

    def auth(self) -> httpx.Auth | None:
        """``api_key`` as ``Authorization: Bearer``, unless ``headers`` has one."""
        return self._key("authorization")

    def _key(self, header: str) -> httpx.Auth | None:
        """``api_key`` in ``header``, or ``None`` without a key."""
        if not self.options.api_key:
            return None
        from privyx.providers._auth import ApiKey

        return ApiKey(self.options.api_key, header)
