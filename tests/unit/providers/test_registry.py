"""The built-in providers, from the registry, and the upstream URL and key a config resolves to."""

from __future__ import annotations

import httpx
import pytest

from privyx import load_config
from privyx.app import Privyx, read_config
from privyx.app._build import build_provider
from privyx.app._serve import build_pipeline
from privyx.core import ConfigError
from privyx.detectors import RegexDetector
from privyx.engine import PrivacyEngine
from privyx.observability.audit import AuditLogger
from privyx.operators import PseudonymOperator
from privyx.policies import DefaultPolicy
from privyx.providers import (
    DEFAULT_BASE_URLS,
    GenericProvider,
    Provider,
    resolve_api_key,
    resolve_base_url,
)
from privyx.proxy._upstream import upstream_client
from privyx.vaults import MemoryVault


def test_registry_builds_generic() -> None:
    provider = Privyx().registry(Provider).build("generic", {"base_url": "http://localhost:9999"})
    assert isinstance(provider, GenericProvider)
    assert provider.base_url == "http://localhost:9999"


def test_gateway_waits_for_the_upstream_as_long_as_the_transparent_proxy() -> None:
    # `privyx proxy --gateway` serves it, and an upstream can take minutes to send
    # its first byte: a flat 60 s timeout turned those requests into a 502.
    assert upstream_client().timeout == httpx.Timeout(
        connect=10.0, read=300.0, write=300.0, pool=10.0
    )


def _engine() -> PrivacyEngine:
    return PrivacyEngine(RegexDetector(), DefaultPolicy(), PseudonymOperator(), MemoryVault())


@pytest.mark.parametrize("ptype", ["generic", "openai", "anthropic"])
async def test_the_gateway_client_follows_the_proxy_settings(ptype: str) -> None:
    proxy = {"mode": "gateway", "timeout": 42, "connect_timeout": 3, "max_connections": 7}
    settings = load_config(extra={"provider": {"type": ptype}, "proxy": proxy})
    gateway = build_pipeline(settings, _engine(), build_provider(settings), AuditLogger())
    assert gateway.gateway
    client = gateway._client  # noqa: SLF001
    assert client.timeout == httpx.Timeout(connect=3.0, read=42.0, write=42.0, pool=10.0)
    assert client._transport._pool._max_connections == 7  # noqa: SLF001
    await gateway.close()


@pytest.mark.parametrize(
    ("failures", "attempts", "outcome"),
    [
        ([httpx.ConnectError] * 2, 3, 200),
        ([httpx.ConnectError] * 3, 3, httpx.ConnectError),
        ([httpx.ConnectTimeout], 1, httpx.ConnectTimeout),  # would wait connect_timeout again
        ([httpx.ReadTimeout], 1, httpx.ReadTimeout),  # the request may have been sent
        ([httpx.RemoteProtocolError], 1, httpx.RemoteProtocolError),
    ],
)
async def test_upstream_client_retries_only_a_connection_that_did_not_open(
    failures: list[type[Exception]],
    attempts: int,
    outcome: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("privyx.proxy._upstream._CONNECT_RETRY_DELAYS", (0.0, 0.0))
    sent: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) <= len(failures):
            raise failures[len(sent) - 1]("")
        return httpx.Response(200)

    client = upstream_client()
    client._transport = httpx.MockTransport(upstream)  # noqa: SLF001
    async with client:
        try:
            result: object = (await client.post("https://up.test/v1/messages", json={})).status_code
        except httpx.HTTPError as exc:
            result = type(exc)

    assert result == outcome
    assert len(sent) == attempts


def test_upstream_client_keeps_the_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A client given its own transport (the usual way to set retries) drops it.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    assert upstream_client()._mounts  # noqa: SLF001


def test_registry_unknown_provider_raises() -> None:
    with pytest.raises(ConfigError, match="unknown provider type 'nonexistent'"):
        Privyx().registry(Provider).build("nonexistent", {})


def test_registry_names() -> None:
    names = Privyx().registry(Provider).builtin_names()
    assert names == ["generic", "openai", "anthropic", "google"]


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
    assert build_provider(settings).url("/v1/chat/completions", stream=False) == (
        "http://127.0.0.1:9999"
    )


def test_a_key_belongs_to_the_provider_it_is_written_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("PRIVYX_API_KEY", "PRIVYX_OPENAI_API_KEY", "PRIVYX_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PRIVYX_OPENAI_API_KEY", "specific")
    warnings: list[str] = []

    def key(provider: object) -> str | None:
        loaded = read_config(extra={"provider": provider})
        warnings.extend(loaded.warnings)
        return resolve_api_key(loaded.settings)

    assert key("openai") == "specific"  # the built-in provider openai's own
    assert key("xai") is None  # another host, of the same type
    assert key({"type": "openai"}) is None  # a provider of its own
    assert any("the key of provider 'openai' is not sent" in w for w in warnings)
    assert key({"type": "openai", "api_key": "own"}) == "own"


def test_privyx_api_key_belongs_to_the_provider_selected_at_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVYX_API_KEY", "from-env")

    assert resolve_api_key(load_config()) == "from-env"
    assert resolve_api_key(load_config(agent="grok")) is None  # its profile's provider
    monkeypatch.setenv("PRIVYX_PROVIDER", "anthropic")
    assert resolve_api_key(load_config()) == "from-env"
    assert resolve_api_key(load_config(agent="codex")) is None


def test_anthropic_provider_sets_the_version_header() -> None:
    provider = build_provider(load_config(extra={"provider": {"type": "anthropic"}}))

    assert provider.headers()["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("ptype", "header", "value"),
    [
        ("generic", "authorization", "Bearer k"),
        ("openai", "authorization", "Bearer k"),
        ("anthropic", "x-api-key", "k"),
        ("google", "x-goog-api-key", "k"),
    ],
)
def test_each_type_sends_its_key_in_its_own_header(ptype: str, header: str, value: str) -> None:
    provider = build_provider(load_config(extra={"provider": {"type": ptype, "api_key": "k"}}))
    request = httpx.Request("POST", "https://up.test/", headers=provider.headers())
    auth = provider.auth()
    assert auth is not None
    sent = next(auth.auth_flow(request))
    assert sent.headers[header] == value
    assert ("authorization" in sent.headers) is (header == "authorization")


def test_a_configured_header_wins_over_the_key() -> None:
    provider = build_provider(
        load_config(
            extra={"provider": {"type": "anthropic", "api_key": "k", "headers": {"X-Api-Key": "h"}}}
        )
    )
    request = httpx.Request("POST", "https://up.test/", headers=provider.headers())
    auth = provider.auth()
    assert auth is not None
    assert next(auth.auth_flow(request)).headers["x-api-key"] == "h"


def test_shipped_example_configs_resolve_to_their_documented_endpoint() -> None:
    """Every shipped example must point where its comments say it points."""
    expectations = {
        "configs/examples/openai.yaml": "https://api.openai.com/v1/chat/completions",
        "configs/examples/anthropic.yaml": "https://api.anthropic.com/v1/messages",
        "configs/examples/custom.yaml": "https://router.example.com",
    }

    for path, expected in expectations.items():
        assert resolve_base_url(load_config(path)) == expected, path
