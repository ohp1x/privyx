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

    1. ``upstream_url`` — a destination other than the provider's own: a
       profile's ``upstream``, or the agent endpoint ``privyx run`` follows.
    2. ``provider.base_url`` — the provider's own (``--upstream`` and
       ``PRIVYX_UPSTREAM_URL`` set it, above the config files).
    3. The provider type's documented default (:data:`DEFAULT_BASE_URLS`).

    Both default to empty precisely so a lower level can be reached; a
    non-empty default at step 1 would make steps 2 and 3 unreachable.
    """
    provider_config = settings.provider
    return (
        settings.upstream_url
        or provider_config.base_url
        or DEFAULT_BASE_URLS.get(provider_config.type, DEFAULT_BASE_URLS["generic"])
    )


def resolve_api_key(settings: Any) -> str | None:
    """Return the upstream key of the provider in use, or ``None``.

    The same in gateway and transparent mode.  ``None`` means it holds no
    key: the gateway sends none, and the transparent proxy relays the
    client's own.
    """
    return settings.provider.api_key or None


def held_credential(settings: Any) -> str | None:
    """What credential of Privyx's own the provider in use sends, if any."""
    if resolve_api_key(settings):
        return "Privyx's own API key"
    # Custom header names can carry credentials too; do not infer their safety.
    if any(settings.provider.headers.values()):
        return "Privyx's configured provider headers"
    return None


def check_credential(settings: Any, *, where: str = "") -> None:
    """Refuse a destination that would take the provider's credential to another origin.

    A credential goes only to the origin of the ``base_url`` it was written
    with, or of its type's default (``settings._home``, set by the loader;
    the provider's own when settings are built directly).  ``where`` names
    the destination when it is not ``upstream_url``'s.

    Raises:
        ConfigError: If the requests would go to another origin.
    """
    held = held_credential(settings)
    if held is None:
        return
    base, kind = settings._home or (settings.provider.base_url, settings.provider.type)
    home = base or DEFAULT_BASE_URLS.get(kind, DEFAULT_BASE_URLS["generic"])
    destination = where or resolve_base_url(settings)
    if _origin(destination) != _origin(home):
        raise ConfigError(
            f"{held} is for {_origin(home)} only, but the requests would go to "
            f"{_origin(destination)}: write that URL beside the key (in its provider, or "
            "PRIVYX_UPSTREAM_URL beside PRIVYX_API_KEY), or define a provider for it under "
            "'providers' with a key of its own and select it"
        )


def _origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, its default port left out, lowercased."""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme.lower()}://{shown}" + (f":{port}" if port and port != default else "")


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

    The origin (:func:`resolve_origin`) unless ``provider.path_mapping.mode``
    is ``prefix``; then the explicit upstream URL itself, path included.

    Raises:
        ConfigError: If prefix mode has no explicit upstream, or one whose
            query, fragment, or credentials a joined path could not keep.
    """
    if settings.provider.path_mapping.mode == "origin":
        return resolve_origin(settings)
    # A provider type's default is a gateway endpoint, not a base to join.
    base: str = settings.upstream_url or settings.provider.base_url
    if not base:
        raise ConfigError(
            "provider.path_mapping.mode prefix needs an upstream URL "
            "(--upstream, or provider.base_url)"
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
            "provider.path_mapping.mode prefix needs an HTTP(S) upstream URL "
            "without query, fragment, or credentials"
        )
    return base.rstrip("/")
