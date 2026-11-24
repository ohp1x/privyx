"""The Anthropic provider: the key in ``x-api-key``, with the API version."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from privyx.config.defaults import DEFAULT_BASE_URLS
from privyx.providers._generic import GenericProvider

if TYPE_CHECKING:
    import httpx

#: The API version sent unless ``headers`` names another.
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(GenericProvider):
    """Messages at ``api.anthropic.com`` by default.

    Anthropic-format endpoints authenticate with ``x-api-key``, not a bearer
    token, the same rule the transparent proxy applies.
    """

    name = "anthropic"
    default_url: ClassVar[str] = DEFAULT_BASE_URLS["anthropic"]

    def headers(self) -> dict[str, str]:
        headers = super().headers()
        if not any(name.lower() == "anthropic-version" for name in headers):
            headers = {"anthropic-version": ANTHROPIC_VERSION, **headers}
        return headers

    def auth(self) -> httpx.Auth | None:
        return self._key("x-api-key")
