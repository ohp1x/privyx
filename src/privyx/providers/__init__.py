"""Providers: where a request goes upstream, and the credential it carries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.providers._base import Provider
from privyx.providers._urls import DEFAULT_BASE_URLS as DEFAULT_BASE_URLS
from privyx.providers._urls import resolve_api_key as resolve_api_key
from privyx.providers._urls import resolve_base_url as resolve_base_url
from privyx.providers._urls import resolve_origin as resolve_origin
from privyx.providers._urls import resolve_transparent_upstream as resolve_transparent_upstream

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.providers._anthropic import AnthropicProvider
    from privyx.providers._auth import ApiKey
    from privyx.providers._generic import GenericProvider
    from privyx.providers._google import GoogleProvider
    from privyx.providers._openai import OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "ApiKey",
    "GenericProvider",
    "GoogleProvider",
    "OpenAIProvider",
    "Provider",
]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "generic": "privyx.providers._generic:GenericProvider",
    "openai": "privyx.providers._openai:OpenAIProvider",
    "anthropic": "privyx.providers._anthropic:AnthropicProvider",
    "google": "privyx.providers._google:GoogleProvider",
}

#: Public names imported on first use: httpx is loaded only when needed.
_LAZY = {**_BUILTINS, "auth": "privyx.providers._auth:ApiKey"}

def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.providers import GoogleProvider`` imports it on first use."""
    return lazy_attribute(name, _LAZY, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in providers with ``app``."""
    app.registry(Provider).builtins(_BUILTINS)
