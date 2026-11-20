"""Where a request goes upstream, resolved from the settings.

Shared by both proxy modes and ``privyx run``: the URL a provider type
defaults to, and the URL and key a config resolves to.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from privyx.core import ConfigError

#: Where each provider type points when nothing else is configured.
DEFAULT_BASE_URLS: dict[str, str] = {
    "generic": "http://localhost:20128",
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "google": "https://generativelanguage.googleapis.com",
}


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


def resolve_transparent_upstream(settings: Any) -> str:
    """Return the URL the transparent proxy appends a request path to.

    The origin (:func:`resolve_origin`) unless ``proxy.path_mapping.mode`` is
    ``prefix``; then the explicit upstream URL itself, path included.

    Raises:
        ConfigError: If prefix mode has no explicit upstream, or one whose
            query, fragment, or credentials a joined path could not keep.
    """
    if settings.proxy.path_mapping.mode == "origin":
        return resolve_origin(settings)
    # A provider type's default is a gateway endpoint, not a base to join.
    base: str = settings.upstream_url or settings.provider.base_url
    if not base:
        raise ConfigError(
            "proxy.path_mapping.mode prefix needs an upstream URL "
            "(--upstream, upstream_url, or provider.base_url)"
        )
    parts = urlsplit(base)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or "@" in parts.netloc
    ):
        raise ConfigError(
            "proxy.path_mapping.mode prefix needs an HTTP(S) upstream URL "
            "without query, fragment, or credentials"
        )
    return base.rstrip("/")
