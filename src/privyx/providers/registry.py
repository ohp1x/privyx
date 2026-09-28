"""Provider registry — build providers from config.

The registry resolves a provider name to a class, keeping the rest of the
codebase free of ``if provider == "openai"`` branches.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from privyx.core.errors import ConfigError
from privyx.plugins.registry import PLUGINS
from privyx.providers.base import Provider
from privyx.providers.generic import GenericProvider

ProviderFactory = Callable[[dict[str, Any]], Provider]

#: Where each provider type points when nothing else is configured.
DEFAULT_BASE_URLS: dict[str, str] = {
    "generic": "http://localhost:20128",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
}

#: The ``proxy`` settings a built-in provider's upstream client takes.
_CLIENT_OPTIONS = ("timeout", "connect_timeout", "max_connections")


class ProviderRegistry:
    """Maps provider names to factory functions."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        self._factories[name] = factory

    def build(self, name: str, config: dict[str, Any]) -> Provider:
        factory = self._factories.get(name)
        if factory is None:
            raise ConfigError(f"unknown provider: {name}")
        return factory(config)

    def names(self) -> list[str]:
        return sorted(self._factories)


def _client_options(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config[key] for key in _CLIENT_OPTIONS if key in config}


def default_registry() -> ProviderRegistry:
    """Registry with the built-in providers."""
    registry = ProviderRegistry()

    def generic_factory(config: dict[str, Any]) -> Provider:
        return GenericProvider(
            base_url=config.get("base_url") or DEFAULT_BASE_URLS["generic"],
            api_key=config.get("api_key"),
            headers=config.get("headers"),
            **_client_options(config),
        )

    def openai_factory(config: dict[str, Any]) -> Provider:
        return GenericProvider(
            base_url=config.get("base_url") or DEFAULT_BASE_URLS["openai"],
            api_key=config.get("api_key"),
            headers=config.get("headers"),
            **_client_options(config),
        )

    def anthropic_factory(config: dict[str, Any]) -> Provider:
        # Anthropic-schema endpoints authenticate with `x-api-key`, not a bearer
        # token, so the key goes in as a header and `api_key` is left unset —
        # the same rule the transparent proxy applies in `proxy.headers`.
        api_key = config.get("api_key")
        headers = {"anthropic-version": "2023-06-01", **config.get("headers", {})}
        if api_key and "x-api-key" not in headers:
            headers["x-api-key"] = api_key
        return GenericProvider(
            base_url=config.get("base_url") or DEFAULT_BASE_URLS["anthropic"],
            headers=headers,
            **_client_options(config),
        )

    registry.register("generic", generic_factory)
    registry.register("openai", openai_factory)
    registry.register("anthropic", anthropic_factory)
    return registry


def resolve_base_url(settings: Any) -> str:
    """Return the upstream URL ``settings`` resolves to.

    Precedence, highest first:

    1. ``upstream_url`` — the explicit override (``--upstream``,
       ``PRIVYX_UPSTREAM_URL``), so the CLI always wins.
    2. ``provider.base_url`` — the endpoint named in the config file.
    3. The provider type's documented default (:data:`DEFAULT_BASE_URLS`).

    Both overrides default to empty precisely so a lower level can be reached;
    a non-empty default at step 1 would make steps 2 and 3 unreachable.
    """
    provider_config = settings.provider
    return (
        settings.upstream_url
        or provider_config.base_url
        or DEFAULT_BASE_URLS.get(provider_config.type, DEFAULT_BASE_URLS["generic"])
    )


def resolve_api_key(settings: Any) -> str | None:
    """Return the upstream key ``settings`` resolves to, or ``None``.

    Precedence, highest first — the same in gateway and transparent mode:

    1. ``provider.<type>_api_key`` (``PRIVYX_OPENAI_API_KEY``, ...), so one
       environment can hold a key per provider type.
    2. ``provider.api_key`` (``PRIVYX_API_KEY``).

    ``None`` means no key is configured: the gateway sends none, and the
    transparent proxy relays the client's own.
    """
    provider_config = settings.provider
    return (
        getattr(provider_config, f"{provider_config.type}_api_key", "")
        or provider_config.api_key
        or None
    )


def resolve_origin(settings: Any) -> str:
    """Return just the ``scheme://host[:port]`` of the resolved upstream.

    The transparent proxy appends the *incoming request path* to this origin, so
    it must not carry a provider-specific chat path.  :func:`resolve_base_url`
    may return a full endpoint (``https://api.openai.com/v1/chat/completions``);
    this strips it back to the origin (``https://api.openai.com``).

    A base URL without a scheme (e.g. ``localhost:20128``) is returned unchanged
    minus any trailing slash, so an explicit ``http://`` origin still round-trips.
    """
    base = resolve_base_url(settings)
    parts = urlsplit(base)
    if parts.scheme and parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return base.rstrip("/")


def build_provider(settings: Any) -> Any:
    """Build the provider described by ``settings``.

    The upstream URL follows :func:`resolve_base_url` and the key
    :func:`resolve_api_key`.

    Raises:
        ConfigError: If ``provider.type`` is not registered.
    """
    provider_config = settings.provider
    ptype = provider_config.type
    config: dict[str, Any] = {
        "api_key": resolve_api_key(settings),
        "headers": dict(provider_config.headers),
        "base_url": resolve_base_url(settings),
    }
    registry = default_registry()
    if ptype in registry.names():
        # Not added to `config` itself: a plugin gets `config` over its own
        # options, and one of those may be called `timeout` too.
        upstream = {key: getattr(settings.proxy, key) for key in _CLIENT_OPTIONS}
        return registry.build(ptype, {**config, **upstream})
    if ptype in PLUGINS.providers:
        # Keys the schema does not define are the plugin's own options.
        options = provider_config.model_extra or {}
        return PLUGINS.providers.build({**options, **config, "type": ptype})
    raise ConfigError(f"unknown provider: {ptype}")
