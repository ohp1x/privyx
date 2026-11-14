"""Agent adapters and the shared, immutable input to preview and execution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType

from privyx.config.schema import AgentConfig, BudgetConfig, Settings
from privyx.core.errors import ConfigError
from privyx.providers.registry import resolve_base_url, resolve_transparent_upstream

AUTO_ANCHOR = "auto"


def claude_args(base_url: str, argv: list[str]) -> list[str]:
    # --settings outranks the tool's settings.json.
    return ["--settings", json.dumps({"env": {"ANTHROPIC_BASE_URL": base_url}}), *argv]


def codex_args(base_url: str, argv: list[str], provider: str | None = None) -> list[str]:
    override = ["-c", f"openai_base_url={json.dumps(base_url)}"]
    if provider:
        # Only the URL changes: the provider keeps its own headers and auth.
        override += ["-c", f"model_providers.{provider}.base_url={json.dumps(base_url)}"]
    # Codex drops -c before a subcommand once another -c follows it.
    end = argv.index("--") if "--" in argv else len(argv)
    own = any(arg.startswith(("-c", "--config")) for arg in argv[:end])
    return [*override, *argv[:end], *(override if own else []), *argv[end:]]


def pi_args(base_url: str, argv: list[str]) -> list[str]:
    # pi reads no base-URL variable; the extension takes PRIVYX_PI_BASE_URL.
    return ["-e", str(Path(__file__).with_name("pi_extension.js")), *argv]


#: For a command with no profile: the base-URL variables most tools read, by
#: provider type, and the path the proxy URL ends in for them.
FALLBACK_ENV: dict[str, tuple[tuple[str, ...], str]] = {
    "openai": (("OPENAI_BASE_URL", "OPENAI_API_BASE"), "/v1"),
    "anthropic": (("ANTHROPIC_BASE_URL",), ""),
    "google": (("GOOGLE_GEMINI_BASE_URL",), ""),
}

ADAPTERS: dict[str, Callable[[str, list[str]], list[str]]] = {
    "claude": claude_args,
    "codex": codex_args,
    "pi": pi_args,
}


def ollama_url(host: str) -> str:
    """The server URL an ``OLLAMA_HOST`` value names, read as Ollama reads it.

    No scheme means ``http`` on port 11434; ``http://`` or ``https://`` without
    a port means 80 or 443; no host, or the any-address a server binds to
    (``0.0.0.0``), means ``127.0.0.1``.
    """
    from urllib.parse import urlsplit

    value = host.strip()
    scheme, sep, rest = value.partition("://")
    port = {"http": 80, "https": 443}.get(scheme, 11434) if sep else 11434
    if not sep:
        scheme, rest = "http", value
    try:
        parts = urlsplit(f"{scheme}://{rest}")
        port = parts.port or port
    except ValueError as exc:
        raise ConfigError("OLLAMA_HOST is not a valid address") from exc
    name = parts.hostname or "127.0.0.1"
    name = "127.0.0.1" if name in ("0.0.0.0", "::") else name
    name = f"[{name}]" if ":" in name else name
    return f"{scheme}://{name}:{port}{parts.path.rstrip('/')}"


@dataclass(frozen=True)
class Target:
    """A tool's launch specification; argv is always passed without a shell."""

    command: str
    provider: str
    env_vars: tuple[str, ...]
    base_path: str = ""
    args: Callable[[str, list[str]], list[str]] | None = None
    extra_env: dict[str, str] = field(default_factory=dict, repr=False)
    env_from: dict[str, str] = field(default_factory=dict)
    default_args: tuple[str, ...] = field(default=(), repr=False)
    adapter: str = "env"
    budget: BudgetConfig = field(default_factory=BudgetConfig)


def profile_target(name: str, profile: AgentConfig) -> Target:
    adapter: str = profile.adapter
    if adapter == "auto":
        candidate = name if name in ADAPTERS else Path(profile.command).name
        adapter = candidate if candidate in ADAPTERS else "env"
    return Target(
        command=profile.command,
        provider=profile.provider,
        env_vars=tuple(profile.env_vars),
        base_path=profile.base_path,
        args=ADAPTERS.get(adapter),
        adapter=adapter,
        extra_env=dict(profile.env),
        env_from=dict(profile.env_from),
        default_args=tuple(profile.args),
        budget=profile.budget,
    )


@dataclass(frozen=True)
class LaunchPlan:
    """Resolved launch inputs, with only the auto-selected port left to bind.

    The environment is snapshotted once, including env_from lookups. Preview
    and subprocess execution use the same argv/environment properties. Binding
    the real port derives a plan without rereading config or the environment.
    """

    target: Target = field(repr=False)
    user_args: tuple[str, ...] = field(repr=False)
    host: str
    port: int
    env_vars: tuple[str, ...]
    inherited_env: Mapping[str, str] = field(repr=False)
    custom_env: Mapping[str, str] = field(repr=False)
    env_sources: Mapping[str, str]
    sources: Mapping[str, str]
    profile: str
    upstream: str = field(repr=False)
    route: str = field(repr=False)
    provider: str
    proxy_mode: str
    session: str
    anchor: str
    native_endpoint: tuple[str, str] | None = field(default=None, repr=False)

    def bind_port(self, port: int) -> LaunchPlan:
        return replace(self, port=port)

    @property
    def command(self) -> str:
        return self.target.command

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port or '<auto>'}{self.target.base_path}"

    @property
    def argv(self) -> list[str]:
        argv = [*self.target.default_args, *self.user_args]
        return self.target.args(self.base_url, argv) if self.target.args else argv

    @property
    def injected_env(self) -> dict[str, str]:
        return {**self.custom_env, **dict.fromkeys(self.env_vars, self.base_url)}

    @property
    def environment(self) -> dict[str, str]:
        return {**self.inherited_env, **self.injected_env}


def _route_codex_provider(
    spec: Target,
    argv: list[str],
    settings: Settings,
    sources: dict[str, str],
    environment: Mapping[str, str],
) -> tuple[Target, tuple[str, str] | None]:
    """Point a custom Codex provider at the proxy, and the proxy at that provider.

    Without this, a ``model_provider`` of the user's own reaches its endpoint
    directly, unmasked. The upstream follows the provider unless one is set
    explicitly; Privyx's own key, when configured, must not reach that host.

    Raises:
        ConfigError: If the Codex config cannot be read or routed.
    """
    from privyx.cli.codex import custom_provider

    found = custom_provider(environment, [*spec.default_args, *argv])
    if found is None:
        return spec, None
    name, base_url = found
    _follow_endpoint(
        spec,
        settings,
        sources,
        (base_url, f"Codex config (model_providers.{name})"),
        label=f"Codex provider {name!r}",
    )
    return replace(spec, args=partial(codex_args, provider=name)), (
        base_url,
        f"Codex config (model_providers.{name})",
    )


def _follow_endpoint(
    spec: Target,
    settings: Settings,
    sources: dict[str, str],
    endpoint: tuple[str, str],
    *,
    label: str = "agent endpoint",
) -> None:
    from urllib.parse import urlsplit

    from privyx.providers.registry import resolve_api_key

    explicit = any(
        value and sources.get(key, "explicit") != "defaults"
        for key, value in (
            ("upstream_url", settings.upstream_url),
            ("provider.base_url", settings.provider.base_url),
        )
    )
    if explicit or settings.proxy.mode != "transparent":
        return
    base_url, origin = endpoint
    if resolve_api_key(settings):
        raise ConfigError(
            f"{label} would receive Privyx's own API key; pass its URL as --upstream to confirm"
        )
    # Custom header names can carry credentials too; do not infer their safety.
    if any(settings.provider.headers.values()):
        raise ConfigError(
            f"{label} would receive Privyx's configured provider headers; "
            "pass its URL as --upstream to confirm"
        )
    try:
        parsed = urlsplit(base_url)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"cannot route {label}: expected an HTTP(S) endpoint")
    # Prefix mapping keeps the endpoint's path; origin mapping drops it.
    if settings.proxy.path_mapping.mode == "origin" and (
        parsed.path.rstrip("/") != spec.base_path.rstrip("/")
    ):
        raise ConfigError(
            f"cannot route {label}: its path must match the agent base_path; "
            "set proxy.path_mapping.mode to prefix, or set base_path to the "
            "endpoint path and configure matching proxy.routes"
        )
    settings.upstream_url = base_url
    sources["upstream_url"] = origin


def build_launch_plan(
    spec: Target,
    argv: list[str],
    settings: Settings,
    port: int,
    env_vars: tuple[str, ...],
    *,
    name: str = "",
    extra_env_vars: tuple[str, ...] = (),
    sources: dict[str, str] | None = None,
) -> LaunchPlan:
    """Resolve references and validate the launch before opening any resources."""
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env_vars):
        raise ConfigError("proxy environment variables must be valid environment variable names")
    inherited = dict(os.environ)
    custom = dict(spec.extra_env)
    provenance = dict(sources or {})
    prefix = f"agents.{name}."
    env_sources = {key: provenance.get(prefix + "env." + key, "profile") for key in custom}
    if spec.adapter == "pi" and "PRIVYX_PI_PROVIDER" not in custom:
        custom["PRIVYX_PI_PROVIDER"] = spec.provider
        env_sources["PRIVYX_PI_PROVIDER"] = provenance.get("provider.type", "defaults")
    for destination, reference in spec.env_from.items():
        if reference not in inherited:
            raise ConfigError(
                f"agent {name or spec.command!r}: env_from.{destination} "
                f"requires environment variable {reference}"
            )
        custom[destination] = inherited[reference]
        origin = provenance.get(prefix + "env_from." + destination, "profile")
        env_sources[destination] = f"{origin}; env_from {reference}"
    environment = {**inherited, **custom}
    mapping = settings.proxy.path_mapping
    if mapping.mode == "prefix":
        # The tool's base URL is the proxy plus base_path; it stands for the
        # upstream URL, so that is the prefix to strip.
        base_path = spec.base_path.rstrip("/")
        if mapping.strip_prefix and mapping.strip_prefix != base_path:
            raise ConfigError(
                f"proxy.path_mapping.strip_prefix: privyx run strips the agent "
                f"base_path ({base_path or '/'}); remove strip_prefix or change base_path"
            )
        mapping.strip_prefix = base_path
    native_endpoint = None
    if spec.adapter == "codex":
        from privyx.cli.codex import openai_endpoint

        spec, native_endpoint = _route_codex_provider(spec, argv, settings, provenance, environment)
        if native_endpoint is None:
            native_endpoint = openai_endpoint(environment, [*spec.default_args, *argv])
    elif spec.adapter == "claude":
        from privyx.cli.claude import endpoint

        native_endpoint = endpoint(environment, [*spec.default_args, *argv])
        if native_endpoint is None and environment.get("ANTHROPIC_BASE_URL"):
            native_endpoint = (environment["ANTHROPIC_BASE_URL"], "environment ANTHROPIC_BASE_URL")
    elif spec.adapter == "env":
        # Multi-provider endpoints and the Gemini login traps cannot identify
        # the model's destination without reading their native configuration.
        candidates = {
            "openai": ("OPENAI_BASE_URL",),
            "aider": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
            "grok": ("GROK_XAI_API_BASE_URL",),
            "gemini": ("GOOGLE_GEMINI_BASE_URL",),
            "ollama": ("OLLAMA_HOST",),
        }.get(Path(spec.command).name, ())
        for key in candidates:
            if environment.get(key):
                value = environment[key]
                native_endpoint = (
                    ollama_url(value) if key == "OLLAMA_HOST" else value,
                    f"environment {key}",
                )
                break
    if native_endpoint:
        value, origin = native_endpoint
        key = origin.removeprefix("environment ")
        if key in env_sources:
            native_endpoint = (value, f"{origin} ({env_sources[key]})")
        _follow_endpoint(spec, settings, provenance, native_endpoint)
    for key in env_vars:
        env_sources[key] = (
            "CLI" if key in extra_env_vars else provenance.get(prefix + "env_vars", "defaults")
        )
    upstream_key = "upstream_url" if settings.upstream_url else "provider.base_url"
    provenance["run.upstream"] = (
        provenance.get(upstream_key, "defaults")
        if settings.upstream_url or settings.provider.base_url
        else f"provider default ({provenance.get('provider.type', 'defaults')})"
    )
    anchor = (
        "auto-provision on launch"
        if settings.anchor.secret == AUTO_ANCHOR
        else "configured (masked)"
        if settings.anchor.secret
        else "disabled"
    )
    return LaunchPlan(
        target=spec,
        user_args=tuple(argv),
        host=settings.host,
        port=port,
        env_vars=tuple(dict.fromkeys(env_vars)),
        inherited_env=MappingProxyType(inherited),
        custom_env=MappingProxyType(custom),
        env_sources=MappingProxyType(env_sources),
        sources=MappingProxyType(provenance),
        profile=name or spec.command,
        upstream=resolve_base_url(settings),
        route=resolve_base_url(settings)
        if settings.proxy.mode == "gateway"
        else resolve_transparent_upstream(settings),
        provider=settings.provider.type,
        proxy_mode=settings.proxy.mode,
        session=settings.session.strategy,
        anchor=anchor,
        native_endpoint=native_endpoint,
    )
