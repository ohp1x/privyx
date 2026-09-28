"""Tests for provider registry and adapters."""

from __future__ import annotations

import httpx
import pytest

from privyx.config.loader import load_config
from privyx.core.errors import ConfigError
from privyx.providers.generic import GenericProvider
from privyx.providers.registry import (
    DEFAULT_BASE_URLS,
    build_provider,
    default_registry,
    resolve_api_key,
    resolve_base_url,
)


def test_registry_builds_generic() -> None:
    registry = default_registry()
    provider = registry.build("generic", {"base_url": "http://localhost:9999"})
    assert isinstance(provider, GenericProvider)
    assert provider._base_url == "http://localhost:9999"  # noqa: SLF001


def test_gateway_waits_for_the_upstream_as_long_as_the_transparent_proxy() -> None:
    # `privyx run` serves the gateway, and an upstream can take minutes to send
    # its first byte: a flat 60 s timeout turned those requests into a 502.
    provider = GenericProvider(base_url="http://localhost:9999")
    assert provider._client.timeout == httpx.Timeout(  # noqa: SLF001
        connect=10.0, read=300.0, write=300.0, pool=10.0
    )


@pytest.mark.parametrize("ptype", ["generic", "openai", "anthropic"])
def test_provider_client_follows_the_proxy_settings(ptype: str) -> None:
    proxy = {"timeout": 42, "connect_timeout": 3, "max_connections": 7}
    provider = build_provider(load_config(extra={"provider": {"type": ptype}, "proxy": proxy}))
    client = provider._client  # noqa: SLF001
    assert client.timeout == httpx.Timeout(connect=3.0, read=42.0, write=42.0, pool=10.0)
    assert client._transport._pool._max_connections == 7  # noqa: SLF001


def test_registry_unknown_provider_raises() -> None:
    registry = default_registry()
    with pytest.raises(ConfigError):
        registry.build("nonexistent", {})


def test_registry_names() -> None:
    registry = default_registry()
    assert "generic" in registry.names()
    assert "openai" in registry.names()
    assert "anthropic" in registry.names()


# --------------------------------------------------------------------------
# base_url precedence
#
# Regression: `upstream_url` used to default to a non-empty localhost URL, so
# it always won and `provider.base_url` was dead config — every example config
# silently pointed at localhost instead of the endpoint it named.


@pytest.mark.parametrize("ptype", ["generic", "openai", "anthropic"])
def test_provider_type_default_url_is_used_when_nothing_is_set(ptype: str) -> None:
    settings = load_config(extra={"provider": {"type": ptype}})

    assert resolve_base_url(settings) == DEFAULT_BASE_URLS[ptype]


def test_config_base_url_beats_the_provider_default() -> None:
    settings = load_config(
        extra={"provider": {"type": "openai", "base_url": "https://proxy.internal/v1"}}
    )

    assert resolve_base_url(settings) == "https://proxy.internal/v1"


def test_upstream_url_beats_config_base_url() -> None:
    """``--upstream`` must always win; it is the most explicit signal there is."""
    settings = load_config(
        extra={
            "upstream_url": "http://127.0.0.1:9999",
            "provider": {"type": "openai", "base_url": "https://proxy.internal/v1"},
        }
    )

    assert resolve_base_url(settings) == "http://127.0.0.1:9999"
    assert build_provider(settings)._base_url == "http://127.0.0.1:9999"  # noqa: SLF001


def test_per_provider_api_key_beats_the_generic_one() -> None:
    settings = load_config(
        extra={"provider": {"type": "openai", "api_key": "generic", "openai_api_key": "specific"}}
    )

    provider = build_provider(settings)

    assert provider._api_key == "specific"  # noqa: SLF001


def test_api_key_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-type key → ``provider.api_key`` → ``None``; ``generic`` has no per-type key."""
    for var in ("PRIVYX_API_KEY", "PRIVYX_OPENAI_API_KEY", "PRIVYX_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def key(**provider: str) -> str | None:
        return resolve_api_key(load_config(extra={"provider": provider}))

    assert key(type="openai", api_key="generic", openai_api_key="specific") == "specific"
    assert key(type="openai", api_key="generic") == "generic"
    assert key(type="generic", openai_api_key="specific") is None
    assert key(type="generic") is None

    monkeypatch.setenv("PRIVYX_API_KEY", "from-env")
    assert key(type="generic") == "from-env"


def test_anthropic_provider_sets_the_version_header() -> None:
    provider = build_provider(load_config(extra={"provider": {"type": "anthropic"}}))

    assert provider._headers["anthropic-version"] == "2023-06-01"  # noqa: SLF001


def test_shipped_example_configs_resolve_to_their_documented_endpoint() -> None:
    """Every shipped example must point where its comments say it points."""
    expectations = {
        "configs/examples/openai.yaml": "https://api.openai.com/v1/chat/completions",
        "configs/examples/anthropic.yaml": "https://api.anthropic.com/v1/messages",
        "configs/examples/custom.yaml": "http://localhost:20128",
    }

    for path, expected in expectations.items():
        assert resolve_base_url(load_config(path)) == expected, path
