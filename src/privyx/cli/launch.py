"""Agent adapters and the shared, immutable input to preview and execution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from privyx.config.schema import AgentConfig, Settings
from privyx.core.errors import ConfigError
from privyx.providers.registry import resolve_base_url, resolve_origin

AUTO_ANCHOR = "__privyx_auto_anchor__"


def claude_args(base_url: str, argv: list[str]) -> list[str]:
    # --settings outranks the tool's settings.json.
    return ["--settings", json.dumps({"env": {"ANTHROPIC_BASE_URL": base_url}}), *argv]


def codex_args(base_url: str, argv: list[str]) -> list[str]:
    override = ["-c", f"openai_base_url={json.dumps(base_url)}"]
    # Codex drops -c before a subcommand once another -c follows it.
    end = argv.index("--") if "--" in argv else len(argv)
    own = any(arg.startswith(("-c", "--config")) for arg in argv[:end])
    return [*override, *argv[:end], *(override if own else []), *argv[end:]]


ADAPTERS: dict[str, Callable[[str, list[str]], list[str]]] = {
    "claude": claude_args,
    "codex": codex_args,
}


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
    for destination, reference in spec.env_from.items():
        if reference not in inherited:
            raise ConfigError(
                f"agent {name or spec.command!r}: env_from.{destination} "
                f"requires environment variable {reference}"
            )
        custom[destination] = inherited[reference]
        origin = provenance.get(prefix + "env_from." + destination, "profile")
        env_sources[destination] = f"{origin}; env_from {reference}"
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
        else resolve_origin(settings),
        provider=settings.provider.type,
        proxy_mode=settings.proxy.mode,
        session=settings.session.strategy,
        anchor=anchor,
    )
