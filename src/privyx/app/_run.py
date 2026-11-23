"""``privyx run``: the launch of an agent, the proxy it runs against, and what its traffic showed.

A target is a profile, a target an agent type spells its own way
(``ollama:<model>``), or a command with no profile.  Where its requests go is
resolved here, with the providers, and given to the plan
(:func:`~privyx.agents._plan.build_launch_plan`), which keeps the agent's
own endpoint from Privyx's credentials.

The agent's requests reach the app ``privyx proxy`` serves, composed the same
way, with an edge middleware of the launch's own, :class:`RequestCounter`,
which counts every HTTP request on every route, and the profile's budget, a
feature.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.agents import Agent
from privyx.agents._env import FALLBACK_ENV
from privyx.agents._exec import can_exec_agent as can_exec_agent
from privyx.agents._exec import exec_agent as exec_agent
from privyx.agents._exec import wait_for_agent_exit as wait_for_agent_exit
from privyx.agents._plan import LaunchPlan, Target, build_launch_plan, profile_target
from privyx.config.agents import profile_type
from privyx.core import PrivyxError

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.config import Loaded
    from privyx.config.schema import BudgetConfig, Settings

#: The counter's edge priority: outermost, so a request refused anywhere counts.
_COUNTER = 1000

_log = logging.getLogger("privyx")


def run_settings(
    app: Privyx,
    provider: str | None = None,
    upstream: str | None = None,
    session_strategy: str | None = None,
    no_anchor: bool = False,
    *,
    harvest: bool | None = None,
    agent: str | None = None,
) -> Loaded:
    """The settings of a launch, its warnings logged.

    ``privyx run`` is opinionated where the library is neutral: unless the user
    says otherwise it treats one conversation as one session and provisions an
    anchor secret, so aliases are stable across turns and restarts out of the
    box.  These are *defaults* (``base_extra``) the user's own config/env still
    overrides; an explicit ``session_strategy`` is a hard override (``extra``).
    The options given to :class:`Privyx` apply, below these flags.
    """
    extra: dict[str, Any] = dict(app._overrides)
    if provider is not None:
        extra["provider"] = provider  # a named provider, over the profile's
    if upstream is not None:
        extra["upstream_url"] = upstream  # the base URL of the provider in use
    base_extra: dict[str, Any] = {"session": {"strategy": "conversation"}}
    if session_strategy is not None:
        extra["session"] = {"strategy": session_strategy}
    if harvest is not None:
        extra["harvest"] = harvest
    if no_anchor:
        # Below the config: an anchor set there still applies.
        base_extra["anchor"] = {"secret": ""}
    return app._read(app._config, extra=extra, base_extra=base_extra, agent=agent).log_warnings()


def expand(app: Privyx, target: str, args: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """A target an agent type spells its own way, as the profile and arguments it means.

    Raises:
        ConfigError: If the target is a type's own but malformed.
    """
    from pydantic import ValidationError

    registry = app.registry(Agent)
    for name in registry.names():
        try:
            agent = registry.get(name)()
        except ValidationError:
            continue  # a type whose options must be given has no syntax of its own
        found = agent.expand(target, args)
        if found is not None:
            return found
    return None


def launch_target(
    app: Privyx,
    settings: Settings,
    name: str,
    *,
    env_vars: tuple[str, ...] = (),
    sources: dict[str, str] | None = None,
) -> Target:
    """The launch specification of profile ``name``, or of a command with no profile.

    Its provider is the one in effect.  A command with no profile, given no
    ``env_vars``, gets the base-URL variables most tools read for that
    provider.

    Raises:
        ConfigError: If its type is unknown or an option invalid.
    """
    registry = app.registry(Agent)
    profile = settings.agents.get(name)
    if profile is not None:
        kind = profile_type(name, profile.model_dump(), registry.names())
        agent = registry.build(kind, profile.model_extra or {}, setting=f"agents.{name}")
        return replace(profile_target(profile, agent), provider=settings.provider.type)
    kind = profile_type(name, {"command": name}, registry.names())
    spec = Target(
        command=name,
        provider=settings.provider.type,
        env_vars=(),
        agent=registry.build(kind, {}, setting=f"agents.{name}"),
    )
    if not env_vars and spec.provider in FALLBACK_ENV:
        names, base_path = FALLBACK_ENV[spec.provider]
        spec = replace(spec, env_vars=names, base_path=base_path)
        if sources is not None:
            sources[f"agents.{name}.env_vars"] = f"unknown command, provider {spec.provider}"
    return spec


def plan_launch(
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
    """The plan of launching ``spec``, with where its requests go resolved by the providers.

    Raises:
        ConfigError: As :func:`~privyx.agents._plan.build_launch_plan`, or if
            the provider's credential would go to another origin than its own.
    """
    from privyx.providers import check_credential, held_credential

    check_credential(settings)
    credentials = held_credential(settings)
    if credentials is None and (warning := _no_credential(settings, name)):
        _log.warning("%s", warning)
    return build_launch_plan(
        spec,
        argv,
        settings,
        port,
        env_vars,
        upstream=_upstream,
        credentials=credentials,
        name=name,
        extra_env_vars=extra_env_vars,
        sources=sources,
    )


def _no_credential(settings: Settings, name: str) -> str | None:
    """Why a launch whose provider holds no key would send no credential, if it would.

    A warning, not an error: an upstream such as a local Ollama takes none.
    """
    if settings.proxy.mode == "gateway":
        return (
            "requests will reach the upstream without a credential: the gateway sends only "
            "the provider's key, and it has none; if the upstream needs one, define a provider "
            "with its base_url and api_key under 'providers' and select it"
        )
    if settings.provider.forward_client_auth:
        return None
    key = f"agents.{name}.privyx.provider" if name in settings.agents else "provider"
    return (
        "requests will reach the upstream without a credential: the provider has no key and "
        f"forward_client_auth is false; write '{key}.forward_client_auth: true' to send the "
        "agent's own"
    )


def _upstream(settings: Settings) -> tuple[str, str]:
    """The upstream URL, and the route the proxy appends a request path to."""
    from privyx.providers import resolve_base_url, resolve_transparent_upstream

    url = resolve_base_url(settings)
    if settings.proxy.mode == "gateway":
        return url, url
    return url, resolve_transparent_upstream(settings)


class RequestCounter:
    """Edge middleware counting the HTTP requests that reach Privyx, on every route.

    Requests that never become an exchange count too, so a tool that ignores
    the proxy URL can be told from one whose requests all failed.
    """

    def __init__(self) -> None:
        self.requests = 0

    def __call__(self, app: Any) -> Any:
        async def counted(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                self.requests += 1
            await app(scope, receive, send)

        return counted


@dataclass(frozen=True)
class RunOutcome:
    """What a launch showed: the agent's exit code, and the traffic Privyx saw."""

    code: int
    #: HTTP requests that reached Privyx, on any route.
    requests: int
    #: When none went upstream on a masking route: how many passed through
    #: unmasked, were blocked, or failed.  ``None`` when one did.
    unproxied: dict[str, int] | None
    #: Whether a request sent a body Privyx relayed unmasked.
    unmasked: bool
    #: Why the budget stopped the agent, after how many turns and tokens.
    stopped: tuple[str, int, int] | None


async def run_agent(
    app: Privyx,
    settings: Settings,
    plan: LaunchPlan,
    sock: socket.socket,
    port: int,
    *,
    budget: BudgetConfig,
    launch: Callable[[LaunchPlan], Awaitable[int]],
    quiet_signals: bool = False,
) -> RunOutcome:
    """Serve ``app`` on ``sock`` for the agent of ``plan``, and report on the run.

    ``launch`` runs the agent once the proxy accepts connections, given the plan
    with its port, and returns its exit code.  With ``quiet_signals``, as in
    the worker of an agent started by exec, uvicorn leaves SIGTERM to the caller.

    Raises:
        ConfigError: If the app cannot be built.
    """
    import uvicorn

    from privyx.app._serve import compose, running
    from privyx.events import (
        AuditEvent,
        ProxyBlocked,
        ProxyError,
        ProxyPassthrough,
        RunUnproxied,
    )

    profile = Path(plan.profile).name  # the base name, so the trail holds no user paths
    # What a request that never went upstream on a masking route came to.
    seen: Counter[str] = Counter()

    def tally(event: AuditEvent) -> None:
        seen[event.name] += 1

    for kind in (ProxyPassthrough, ProxyBlocked, ProxyError):
        app._events.add(kind, tally)
    features: dict[str, dict[str, Any]] = {}
    if budget.max_turns or budget.max_tokens:
        features["budget"] = {**budget.model_dump(), "profile": profile}
    try:
        counter = RequestCounter()
        app.add_middleware(counter, priority=_COUNTER)
        asgi, router = compose(app, settings, lifespan=False)
        async with running(
            app, settings, router, features=features, run_token=plan.run_token
        ) as pipeline:
            audit = pipeline.audit
            server: Any = uvicorn.Server(
                uvicorn.Config(
                    asgi,
                    host=settings.server.host,
                    port=port,
                    log_level="warning",  # the tool owns the terminal; stay quiet
                    ws="none",  # a WebSocket upgrade arrives as a plain HTTP request
                    log_config=None,  # uvicorn's records go to Privyx's handlers, not stderr
                )
            )
            if quiet_signals:
                server.capture_signals = nullcontext
            serve_task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                await _wait_until_started(server, serve_task)
                code = await launch(plan.bind_port(port))
                # Drain requests still in flight first: one being masked when the
                # tool exited may yet go out on a masking route.
                server.should_exit = True
                await serve_task
                unproxied: dict[str, Any] | None = None
                if not audit.routed:
                    # What a tool that ignores the proxy URL looks like from here, or
                    # one whose model requests take a path outside proxy.routes.
                    unproxied = {
                        "passthrough": seen[ProxyPassthrough.name],
                        "blocked": seen[ProxyBlocked.name],
                        "failed": seen[ProxyError.name],
                    }
                    audit.emit(
                        RunUnproxied,
                        profile=profile,
                        requests=counter.requests,
                        **unproxied,
                    )
                stopped: Any = app.features.get("budget")
                return RunOutcome(
                    code=code,
                    requests=counter.requests,
                    unproxied=unproxied,
                    unmasked=bool(audit.unmasked_bodies),
                    stopped=(stopped.exceeded, stopped.turns, stopped.tokens)
                    if stopped is not None and stopped.exceeded
                    else None,
                )
            finally:
                server.should_exit = True
                await serve_task
    finally:
        sock.close()
        app._events.remove(tally)


async def _wait_until_started(server: Any, serve_task: asyncio.Task[Any]) -> None:
    """Block until uvicorn reports it is accepting connections.

    Raises:
        RuntimeError: If the server task exits before becoming ready — e.g. the
            port is taken.  Without this the tool would launch against a dead
            proxy and fail with a confusing connection error.
    """
    while not server.started:
        if serve_task.done():
            await serve_task  # re-raise the startup failure
            raise RuntimeError("proxy exited before it finished starting")
        await asyncio.sleep(0.02)


def listen(host: str, port: int) -> tuple[socket.socket, int]:
    """Bind the proxy's socket on ``port`` (0 picks a free one); return it and its port.

    Raises:
        PrivyxError: If the port cannot be bound, e.g. another process uses it.
    """
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn's own bind does
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise PrivyxError(f"cannot listen on {host}:{port}: {exc.strerror or exc}") from exc
    return sock, int(sock.getsockname()[1])


async def spawn(plan: LaunchPlan) -> int:
    """Run the tool as a child process with the proxy URL injected.

    Signals are left to the child: it owns the terminal, so Ctrl-C reaches it
    directly and this process just waits for the exit code.
    """
    process = await asyncio.create_subprocess_exec(plan.command, *plan.argv, env=plan.environment)
    try:
        return await process.wait()
    except asyncio.CancelledError:  # pragma: no cover - interactive only
        process.terminate()
        await process.wait()
        raise
