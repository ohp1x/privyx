"""A launch planned before anything opens: the shared, immutable input to preview and execution.

The agent type (:class:`~privyx.agents.Agent`) knows the tool.  What every
launch keeps is here: the proxy URL in ``env_vars``, Privyx's own credentials
kept from the tool's endpoint (:func:`_follow_endpoint`), and argv passed
without a shell.  Where requests go is resolved by the caller, which knows
the providers, and given here as arguments.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from privyx.agents._base import Agent, Endpoint, LaunchContext
from privyx.agents._env import EnvAgent
from privyx.config.schema import AgentConfig, BudgetConfig, Settings
from privyx.core import ConfigError

AUTO_ANCHOR = "auto"


@dataclass(frozen=True)
class Target:
    """A tool's launch specification; argv is always passed without a shell."""

    command: str
    provider: str
    env_vars: tuple[str, ...]
    #: The tool's type, built with the profile's options for it.
    agent: Agent = field(default_factory=EnvAgent)
    base_path: str = ""
    extra_env: dict[str, str] = field(default_factory=dict, repr=False)
    env_from: dict[str, str] = field(default_factory=dict)
    default_args: tuple[str, ...] = field(default=(), repr=False)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    @property
    def type(self) -> str:
        """The agent type's name, as a profile's ``type`` writes it."""
        return self.agent.name


def profile_target(profile: AgentConfig, agent: Agent) -> Target:
    """The launch specification of ``profile``, whose type ``agent`` is built."""
    return Target(
        command=profile.command,
        provider=profile.provider,
        env_vars=tuple(profile.env_vars),
        agent=agent,
        base_path=profile.base_path,
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
    native_endpoint: Endpoint | None = field(default=None, repr=False)
    #: Set when each request names its upstream (pi); see privyx.proxy.Pipeline.
    run_token: str | None = field(default=None, repr=False)

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
        return self.target.agent.argv(self.base_url, argv, self.native_endpoint)

    @property
    def injected_env(self) -> dict[str, str]:
        return {**self.custom_env, **dict.fromkeys(self.env_vars, self.base_url)}

    @property
    def environment(self) -> dict[str, str]:
        return {**self.inherited_env, **self.injected_env}


def _can_follow(settings: Settings, sources: Mapping[str, str]) -> bool:
    """Whether the upstream may follow the agent: transparent, none set explicitly."""
    explicit = any(
        value and sources.get(key, "explicit") != "defaults"
        for key, value in (
            ("upstream_url", settings.upstream_url),
            ("provider.base_url", settings.provider.base_url),
        )
    )
    return not explicit and settings.proxy.mode == "transparent"


def _follow_endpoint(
    spec: Target,
    settings: Settings,
    sources: dict[str, str],
    endpoint: Endpoint,
    credentials: str | None,
    *,
    label: str = "agent endpoint",
) -> None:
    """Send requests where the tool would on its own, unless an upstream is set explicitly.

    Raises:
        ConfigError: If Privyx's own ``credentials`` would reach that endpoint,
            or it is not an HTTP(S) URL its path mapping can join.
    """
    from urllib.parse import urlsplit

    if not _can_follow(settings, sources):
        return
    if credentials:
        raise ConfigError(
            f"{label} would receive {credentials}; define a provider for "
            f"{endpoint.url} under 'providers', with a key of its own, and select it, "
            "or select a provider without a key of Privyx's"
        )
    try:
        parsed = urlsplit(endpoint.url)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"cannot route {label}: expected an HTTP(S) endpoint")
    # Prefix mapping keeps the endpoint's path; origin mapping drops it.
    if settings.provider.path_mapping.mode == "origin" and (
        parsed.path.rstrip("/") != spec.base_path.rstrip("/")
    ):
        raise ConfigError(
            f"cannot route {label}: its path must match the agent base_path; "
            "set provider.path_mapping.mode to prefix, or set base_path to the "
            "endpoint path and configure matching proxy.routes"
        )
    settings.upstream_url = endpoint.url
    sources["upstream_url"] = endpoint.origin


def build_launch_plan(
    spec: Target,
    argv: list[str],
    settings: Settings,
    port: int,
    env_vars: tuple[str, ...],
    *,
    upstream: Callable[[Settings], tuple[str, str]],
    credentials: str | None = None,
    name: str = "",
    extra_env_vars: tuple[str, ...] = (),
    sources: dict[str, str] | None = None,
) -> LaunchPlan:
    """Resolve references and validate the launch before opening any resources.

    Args:
        upstream: Where requests go once the tool's endpoint is followed:
            the upstream URL and the route the proxy appends a path to.
        credentials: Which credentials of Privyx's own every upstream would
            receive, if any: then the tool's endpoint is not followed.

    Raises:
        ConfigError: If a variable is missing or misnamed, or the tool's
            endpoint cannot be followed.
    """
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in env_vars):
        raise ConfigError("proxy environment variables must be valid environment variable names")
    inherited = dict(os.environ)
    custom = dict(spec.extra_env)
    provenance = dict(sources or {})
    prefix = f"agents.{name}."
    env_sources = {key: provenance.get(prefix + "env." + key, "profile") for key in custom}
    for destination, reference in spec.env_from.items():
        if reference not in inherited:
            raise ConfigError(
                f"agent {name or spec.command!r}: env_from.{destination} "
                f"requires environment variable {reference}"
            )
        custom[destination] = inherited[reference]
        origin = provenance.get(prefix + "env_from." + destination, "profile")
        env_sources[destination] = f"{origin}; env_from {reference}"
    agent = spec.agent
    context = LaunchContext(
        command=spec.command,
        argv=(*spec.default_args, *argv),
        environment={**inherited, **custom},
        provider=spec.provider,
    )
    run_token = None
    # After env_from, which can choose the one upstream too: what the type sets
    # for one upstream, a profile that sets it itself keeps.
    token = agent.upstream_per_request
    single = agent.env(context)
    if (
        token is not None
        and not single.keys() & custom.keys()
        and _can_follow(settings, provenance)
        and not credentials
    ):
        # Each provider of the tool goes to its own base URL, which it names
        # per request; with credentials of Privyx's own for them all, one, as before.
        for key in single:
            inherited.pop(key, None)
        run_token = secrets.token_urlsafe(32)
        custom[token] = run_token
        env_sources[token] = "new each run"
        context = replace(context, environment={**inherited, **custom}, per_request=True)
    for key, value in agent.env(context).items():
        if key not in custom:
            # A type's variables follow the provider in effect, as pi's does.
            custom[key] = value
            env_sources[key] = provenance.get("provider.type", "defaults")
    mapping = settings.provider.path_mapping
    if mapping.mode == "prefix":
        # The tool's base URL is the proxy plus base_path; it stands for the
        # upstream URL, so that is the prefix to strip.
        base_path = spec.base_path.rstrip("/")
        if mapping.strip_prefix and mapping.strip_prefix != base_path:
            raise ConfigError(
                f"provider.path_mapping.strip_prefix: privyx run strips the agent "
                f"base_path ({base_path or '/'}); remove strip_prefix or change base_path"
            )
        mapping.strip_prefix = base_path
    native_endpoint = agent.endpoint(replace(context, environment={**inherited, **custom}))
    if native_endpoint is not None:
        key = native_endpoint.origin.removeprefix("environment ")
        if key in env_sources:
            native_endpoint = replace(
                native_endpoint, origin=f"{native_endpoint.origin} ({env_sources[key]})"
            )
        label = (
            f"{agent.name.capitalize()} provider {native_endpoint.provider!r}"
            if native_endpoint.provider
            else "agent endpoint"
        )
        _follow_endpoint(spec, settings, provenance, native_endpoint, credentials, label=label)
    for key in env_vars:
        env_sources[key] = (
            "CLI" if key in extra_env_vars else provenance.get(prefix + "env_vars", "defaults")
        )
    upstream_key = "upstream_url" if settings.upstream_url else "provider.base_url"
    provenance["run.upstream"] = (
        agent.name
        if run_token
        else provenance.get(upstream_key, "defaults")
        if settings.upstream_url or settings.provider.base_url
        else f"provider default ({provenance.get('provider.type', 'defaults')})"
    )
    secret = settings.anchor.model_dump().get("secret", AUTO_ANCHOR)
    anchor = (
        "auto-provision on launch"
        if secret == AUTO_ANCHOR
        else "configured (masked)"
        if secret
        else "disabled"
    )
    each = f"the provider of each {agent.name} model"
    resolved, route = (each, each) if run_token else upstream(settings)
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
        upstream=resolved,
        route=route,
        provider=settings.provider.type,
        proxy_mode=settings.proxy.mode,
        session=settings.session.strategy,
        anchor=anchor,
        native_endpoint=native_endpoint,
        run_token=run_token,
    )
