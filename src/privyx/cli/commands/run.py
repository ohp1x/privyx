"""`privyx run` command — run a downstream tool through the Privyx proxy.

``privyx run claude`` wires::

    Claude Code  →  Privyx  →  Anthropic

by starting the proxy on a local port and launching the tool pointed at that
port: its base-URL environment variable, plus an argument for a tool whose own
config outranks that variable.  The tool is unmodified and unaware;
it just talks to what it thinks is the provider.  The transparent proxy (the
default) relays the tool's own paths and headers, its key or login included;
``proxy.mode: gateway`` serves the gateway, which sends only Privyx's own key.

This is orchestration only (principle #12): no privacy logic lives here.  The
engine and proxy both come from the same builders ``privyx proxy`` uses, so
both commands behave identically.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import sys
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from click.core import ParameterSource

from privyx.cli.launch import (
    AUTO_ANCHOR,
    FALLBACK_ENV,
    LaunchPlan,
    Target,
    build_launch_plan,
    codex_args,
    ollama_url,
    profile_target,
)
from privyx.cli.native import can_exec_agent, exec_agent, wait_for_agent_exit
from privyx.config.defaults import DEFAULT_AGENTS
from privyx.config.schema import AgentConfig

if TYPE_CHECKING:
    from privyx.config.schema import Settings

TARGETS: dict[str, Target] = {
    name: profile_target(name, AgentConfig.model_validate(profile))
    for name, profile in DEFAULT_AGENTS.items()
}


def _run_settings(
    config_path: str | None,
    provider: str | None,
    upstream: str | None,
    session_strategy: str | None,
    no_anchor: bool,
    *,
    harvest: bool | None = None,
    agent: str | None = None,
    sources: dict[str, str] | None = None,
) -> Settings:
    from privyx.config.loader import load_config

    extra: dict[str, Any] = {}
    if provider is not None:
        extra["provider"] = {"type": provider}
    if upstream is not None:
        extra["upstream_url"] = upstream
        extra.setdefault("provider", {})["base_url"] = upstream
    base_extra: dict[str, Any] = {"session": {"strategy": "conversation"}}
    if session_strategy is not None:
        extra["session"] = {"strategy": session_strategy}
    if harvest is not None:
        extra["harvest"] = harvest
    if not no_anchor:
        # Resolved only when running, so inspection never creates anchor.key.
        base_extra["anchor"] = {"secret": AUTO_ANCHOR}
    return load_config(
        config_path, extra=extra, base_extra=base_extra, agent=agent, sources=sources
    )


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("target", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.option("--provider", "-p", default=None, help="Override the upstream provider type")
@click.option("--upstream", "-u", default=None, help="Override the upstream URL")
@click.option(
    "--port",
    default=0,
    type=click.IntRange(0, 65535),
    help="Proxy port (0 = pick a free one)",
)
@click.option(
    "--env-var",
    "env_vars",
    multiple=True,
    help="Extra environment variable to set to the proxy URL (repeatable)",
)
@click.option(
    "--session-strategy",
    type=click.Choice(["ephemeral", "client", "conversation"]),
    default=None,
    help="Session identity for header-less clients (default: conversation).",
)
@click.option(
    "--no-anchor",
    is_flag=True,
    default=False,
    help="Don't auto-provision an anchor secret; leave pseudonym anchoring to config.",
)
@click.option(
    "--harvest/--no-harvest",
    default=None,
    help="Also mask this workspace's git authors, remotes, and .env values (default: config).",
)
@click.option("--list", "list_targets", is_flag=True, help="List known targets and exit")
@click.option("--dry-run", is_flag=True, help="Show the launch plan without starting anything")
def run(
    target: str | None,
    args: tuple[str, ...],
    config_path: str | None,
    provider: str | None,
    upstream: str | None,
    port: int,
    env_vars: tuple[str, ...],
    session_strategy: str | None,
    no_anchor: bool,
    harvest: bool | None,
    list_targets: bool,
    dry_run: bool,
) -> None:
    """Run TARGET with its traffic routed through Privyx.

    Examples:

    \b
      privyx run claude
      privyx run codex --provider openai
      privyx run --env-var MY_TOOL_BASE_URL -- my-tool --flag
    """
    from privyx.core.errors import PrivyxError

    if not target and not list_targets:
        raise click.UsageError("missing TARGET (try `privyx run --list`)")

    sources: dict[str, str] = {}
    try:
        settings = _run_settings(
            config_path,
            provider,
            upstream,
            session_strategy,
            no_anchor,
            harvest=harvest,
            agent=target,
            sources=sources,
        )
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc

    if list_targets:
        click.echo("Known targets:")
        for name, listed in sorted(settings.agents.items()):
            click.echo(f"  {name:<8} provider={listed.provider:<10} {', '.join(listed.env_vars)}")
        return

    assert target is not None
    try:
        ollama = _ollama_launch(target, args, upstream, settings, sources)
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    if ollama is not None:
        target, args, upstream, upstream_from_env = ollama
        sources = {}
        try:
            settings = _run_settings(
                config_path,
                provider,
                upstream,
                session_strategy,
                no_anchor,
                harvest=harvest,
                agent=target,
                sources=sources,
            )
        except PrivyxError as exc:
            raise click.ClickException(str(exc)) from exc
        if upstream is not None and upstream_from_env:
            sources["upstream_url"] = "environment OLLAMA_HOST"
    profile = settings.agents.get(target)
    spec = (
        profile_target(target, profile)
        if profile
        else Target(command=target, provider=settings.provider.type, env_vars=())
    )
    spec = replace(spec, provider=settings.provider.type)
    if not profile and not env_vars and spec.provider in FALLBACK_ENV:
        # A tool Privyx has no profile for: the variables most tools read.
        names, base_path = FALLBACK_ENV[spec.provider]
        spec = replace(spec, env_vars=names, base_path=base_path)
        sources[f"agents.{target}.env_vars"] = f"unknown command, provider {spec.provider}"
    all_env_vars = tuple(spec.env_vars) + tuple(env_vars)
    if not all_env_vars and spec.args is None:
        import difflib
        import re

        base = re.split(r"[:-]", target, maxsplit=1)[0]
        near = (
            [base]
            if base != target and base in settings.agents
            else difflib.get_close_matches(target, list(settings.agents), n=1)
        )
        hint = f" (did you mean {near[0]!r}?)" if near else ""
        raise click.UsageError(
            f"no agent profile {target!r}{hint}; define it under `agents` "
            "or, to launch a tool directly, pass --provider openai or anthropic "
            "(sets the usual base-URL variables) or --env-var NAME "
            "(see `privyx run --list`)"
        )

    sources["run.port"] = (
        "CLI"
        if click.get_current_context().get_parameter_source("port") == ParameterSource.COMMANDLINE
        else "run defaults"
    )
    try:
        if spec.adapter == "codex":
            spec = _route_codex_provider(spec, list(args), settings, sources)
        plan = build_launch_plan(
            spec,
            list(args),
            settings,
            port,
            all_env_vars,
            name=target,
            extra_env_vars=env_vars,
            sources=sources,
        )
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    harvested = _harvest(settings) if settings.harvest else None
    if dry_run:
        _show_plan(plan)
        if harvested is not None:
            click.echo(f"Harvest: {harvested}")
        return
    if harvested is not None:
        click.echo(f"Harvest: {harvested}", err=True)

    if shutil.which(plan.command, path=plan.environment.get("PATH")) is None:
        click.echo(f"Error: {spec.command!r} not found on PATH", err=True)
        sys.exit(127)
    if Path(plan.command).name == "ollama":
        _check_ollama(plan.upstream)

    target_run = partial(
        _run_target,
        spec=spec,
        argv=list(args),
        config_path=config_path,
        upstream=upstream,
        port=port,
        env_vars=all_env_vars,
        session_strategy=session_strategy,
        no_anchor=no_anchor,
        settings=settings,
        plan=plan,
    )
    try:
        if can_exec_agent():
            exec_agent(
                plan,
                lambda ready, parent: target_run(ready=ready, parent=parent),
                _show_running,
            )
        code = asyncio.run(target_run())
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    sys.exit(code)


def _ollama_launch(
    target: str,
    args: tuple[str, ...],
    upstream: str | None,
    settings: Settings,
    sources: dict[str, str],
) -> tuple[str, tuple[str, ...], str | None, bool] | None:
    """``ollama:<model>`` as ``ollama run <model>``, and the server it goes to.

    A profile of the user's own by that name wins.  The ``ollama`` profile goes
    to the server ``OLLAMA_HOST`` names, as the tool would, unless
    ``--upstream`` or a configured profile upstream says otherwise; for the
    launch, the tool's own ``OLLAMA_HOST`` then points at Privyx.  Returns the
    new target, arguments, upstream, and whether the upstream came from
    ``OLLAMA_HOST``, or ``None`` when nothing changes.

    Raises:
        ConfigError: If ``OLLAMA_HOST`` is not an address.
    """
    expanded = target.startswith("ollama:") and target not in settings.agents
    if expanded:
        model = target.removeprefix("ollama:")
        if not model:
            raise click.UsageError("name a model after ollama:, as in ollama:llama3.2")
        target, args = "ollama", ("run", model, *args)
    host = os.environ.get("OLLAMA_HOST", "").strip()
    from_env = (
        target == "ollama"
        and upstream is None
        and bool(host)
        and sources.get("agents.ollama.upstream", "defaults") == "defaults"
    )
    if from_env:
        upstream = ollama_url(host)
    if not (expanded or from_env):
        return None
    return target, args, upstream, from_env


def _check_ollama(upstream: str) -> None:
    """Exit with a hint when no Ollama server answers at ``upstream``.

    Any HTTP answer will do: a server behind an authenticating proxy may
    answer this one with a ``401`` and still serve the tool.
    """
    import httpx

    url = upstream.rstrip("/") + "/api/version"
    try:
        httpx.get(url, timeout=3.0)
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        click.echo(
            f"Error: no Ollama server answers at {upstream} ({type(exc).__name__}); "
            "start one with `ollama serve`, or name another with --upstream or OLLAMA_HOST",
            err=True,
        )
        sys.exit(1)


def _harvest(settings: Settings) -> str:
    """Add a detector for what the working directory tells; return what it masks.

    A detector of its own, so a ``patterns`` entry of the user's for the same
    entity cannot override it.  Only counts are returned: the values are
    never shown or written.
    """
    import re

    from privyx.cli import workspace
    from privyx.config.schema import DetectorConfig

    root = Path.cwd()
    terms: dict[str, list[str]] = {}
    for item in workspace.unique(workspace.git_terms(root, workspace.HARVEST_COMMITS)):
        terms.setdefault(item.entity, []).append(item.value)
    values: dict[str, set[str]] = {}
    for key, value, _ in workspace.env_values(root):
        entity = workspace.env_entity(key)
        if entity:
            values.setdefault(entity, set()).add(re.escape(value))
    # Literal and case-sensitive, as a secret is: the fastest regex to search.
    patterns = {e: "|".join(sorted(v, key=lambda p: (-len(p), p))) for e, v in values.items()}
    if terms or patterns:
        current = settings.detector if isinstance(settings.detector, list) else [settings.detector]
        added = DetectorConfig(type="yaml", terms=terms, patterns=patterns)
        settings.detector = [*current, added]
    counts = [f"{len(v)} {entity}" for entity, v in sorted(terms.items())]
    counts.append(f"{sum(map(len, values.values()))} .env value(s)")
    return ", ".join(counts)


def _route_codex_provider(
    spec: Target, argv: list[str], settings: Settings, sources: dict[str, str]
) -> Target:
    """Point a custom Codex provider at the proxy, and the proxy at that provider.

    Without this, a ``model_provider`` of the user's own reaches its endpoint
    directly, unmasked. The upstream follows the provider unless one is set
    explicitly; Privyx's own key, when configured, must not reach that host.

    Raises:
        ConfigError: If the Codex config cannot be read or routed.
    """
    from urllib.parse import urlsplit

    from privyx.cli.codex import custom_provider
    from privyx.core.errors import ConfigError
    from privyx.providers.registry import resolve_api_key

    found = custom_provider({**os.environ, **spec.extra_env}, [*spec.default_args, *argv])
    if found is None:
        return spec
    name, base_url = found
    explicit = settings.upstream_url or settings.provider.base_url
    if settings.proxy.mode == "transparent" and not explicit:
        if resolve_api_key(settings):
            raise ConfigError(
                f"Codex provider {name!r} would receive Privyx's own API key; "
                "pass its URL as --upstream to confirm"
            )
        if urlsplit(base_url).path.rstrip("/") != spec.base_path:
            raise ConfigError(
                f"cannot route Codex provider {name!r}: Privyx forwards Codex requests "
                f"under {spec.base_path}, and its base_url is {base_url}"
            )
        settings.upstream_url = base_url
        sources["upstream_url"] = f"Codex config (model_providers.{name})"
    return replace(spec, args=partial(codex_args, provider=name))


def _show_running(plan: LaunchPlan) -> None:
    from privyx.config.redact import redact, redact_argv

    click.echo(f"Privyx proxy → {redact(plan.route, 'url')}", err=True)
    click.echo(f"Running: {shlex.join(redact_argv([plan.command, *plan.argv]))}", err=True)
    click.echo(f"  {', '.join(plan.env_vars)} = {plan.base_url}", err=True)
    _set_terminal_title(plan.profile)


def _show_plan(plan: LaunchPlan) -> None:
    from privyx.config.redact import redact, redact_argv

    sources = plan.sources
    click.echo(f"Profile: {plan.profile}")
    click.echo(f"Command: {shlex.join(redact_argv([plan.command, *plan.argv]))}")
    click.echo(f"Adapter: {plan.target.adapter}")
    click.echo(f"Proxy: {plan.base_url} ({plan.proxy_mode})")
    click.echo(f"Provider: {plan.provider} [{sources.get('provider.type', 'defaults')}]")
    click.echo(f"Upstream: {redact(plan.upstream, 'upstream_url')} [{sources['run.upstream']}]")
    click.echo(f"Routing: tool → {plan.base_url} → {redact(plan.route, 'upstream_url')}")
    click.echo(f"Session: {plan.session} [{sources.get('session.strategy', 'defaults')}]")
    click.echo(f"Anchor: {plan.anchor} [{sources.get('anchor.secret', 'defaults')}]")
    budget = plan.target.budget
    limits = [f"{budget.max_turns} turns"] if budget.max_turns else []
    limits += [f"{budget.max_tokens} tokens"] if budget.max_tokens else []
    click.echo(f"Budget: {', '.join(limits) or 'none'}")
    prefix = f"agents.{plan.profile}."
    for field_name in ("command", "base_path", "adapter", "args"):
        fallback = "CLI" if field_name == "command" else "defaults"
        click.echo(f"Source {field_name}: {sources.get(prefix + field_name, fallback)}")
    for path in ("host", "proxy.mode", "run.port"):
        click.echo(f"Source {path}: {sources.get(path, 'defaults')}")
    click.echo("Source arguments: CLI")
    click.echo("Environment injected:")
    for key in sorted(plan.injected_env):
        value = plan.base_url if key in plan.env_vars else "***"
        click.echo(f"  {key}={value} [{plan.env_sources[key]}]")


async def _run_target(
    spec: Target,
    argv: list[str],
    config_path: str | None,
    upstream: str | None,
    port: int,
    env_vars: tuple[str, ...],
    session_strategy: str | None = None,
    no_anchor: bool = False,
    settings: Settings | None = None,
    plan: LaunchPlan | None = None,
    ready: socket.socket | None = None,
    parent: int | None = None,
) -> int:
    """Start the proxy, run the tool against it, and tear everything down.

    ``privyx run`` is opinionated where the library is neutral: unless the user
    says otherwise it treats one conversation as one session and provisions an
    anchor secret, so aliases are stable across turns and restarts out of the
    box.  These are *defaults* (``base_extra``) the user's own config/env still
    overrides; an explicit ``--session-strategy`` is a hard override (``extra``).
    ``proxy.mode`` picks the app, as in ``privyx proxy``.

    Returns:
        The tool's exit code.
    """
    from privyx.cli.budget import Budget
    from privyx.cli.commands.proxy import build_transparent_proxy
    from privyx.core.builder import build_audit_logger, build_engine
    from privyx.gateway.server import Gateway
    from privyx.gateway.transparent import create_transparent_app
    from privyx.observability import tracing
    from privyx.observability.audit import AuditEventType
    from privyx.observability.logging import configure_logging
    from privyx.plugins.loader import load_plugins
    from privyx.providers.registry import build_provider

    if settings is None:
        settings = _run_settings(config_path, spec.provider, upstream, session_strategy, no_anchor)
    if plan is None:
        plan = build_launch_plan(spec, argv, settings, port, env_vars)
    # The child tool owns the terminal: log lines go to log_file if one is set,
    # else nowhere, and the audit trail still records every error.
    configure_logging(settings, console=False)
    # Bound before the rest opens, so a port in use leaves nothing to undo.
    sock, bound_port = _listen(settings.host, port)

    hooks = load_plugins(settings)
    tracing.configure(settings.tracing.enabled)
    audit = build_audit_logger(settings)
    engine, close_vault = await build_engine(settings, audit=audit)
    if settings.proxy.mode == "gateway":
        provider = build_provider(settings)
        app = Gateway(engine=engine, provider=provider, settings=settings, audit=audit).app
        close_upstream = provider.close
    else:
        proxy = build_transparent_proxy(settings, engine, audit)
        app = create_transparent_app(engine, proxy, settings)
        close_upstream = proxy.close
    await hooks.run_startup()

    budget = spec.budget
    guard = (
        Budget(app, budget, settings.proxy.routes, audit, Path(plan.profile).name)
        if budget.max_turns or budget.max_tokens
        else None
    )
    counted = _Counted(guard or app)
    server = _make_server(counted, settings, bound_port)
    if parent is not None:
        # The worker handles SIGTERM itself; uvicorn must not re-raise it during cleanup.
        server.capture_signals = nullcontext
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _wait_until_started(server, serve_task)
        plan = plan.bind_port(bound_port)

        # On stderr: stdout is the tool's, and may be piped into a parser.
        if ready is not None and parent is not None:
            ready.sendall(json.dumps({"port": bound_port}).encode() + b"\n")
            ready.close()
            await wait_for_agent_exit(parent)
            code = 0
        else:
            _show_running(plan)
            code = await _spawn(plan)
        if not counted.requests:
            # What a tool that ignores the proxy URL looks like from here.
            # The profile's base name, so the trail holds no user paths.
            audit.emit(AuditEventType.RUN_UNPROXIED, profile=Path(plan.profile).name)
            # After exec the shell has likely drawn its prompt already.
            click.echo(
                ("\n" if parent is not None else "")
                + f"Warning: no request from {spec.command} reached Privyx; if it called "
                "its provider, it did so directly, without masking.",
                err=True,
            )
        if guard is not None and guard.exceeded:
            click.echo(
                ("\n" if parent is not None else "")
                + f"Privyx stopped {spec.command}: the {guard.exceeded}, "
                f"after {guard.turns} turn(s) and {guard.tokens} tokens.",
                err=True,
            )
        return code
    finally:
        server.should_exit = True
        await serve_task
        await hooks.run_shutdown()
        await close_vault()
        await close_upstream()
        audit.close()


class _Counted:
    """An ASGI app that counts the HTTP requests reaching ``app``."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.requests += 1
        await self.app(scope, receive, send)


def _listen(host: str, port: int) -> tuple[socket.socket, int]:
    """Bind the proxy's socket on ``port`` (0 picks a free one); return it and its port.

    Raises:
        PrivyxError: If the port cannot be bound, e.g. another process uses it.
    """
    from privyx.core.errors import PrivyxError

    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn's own bind does
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise PrivyxError(f"cannot listen on {host}:{port}: {exc.strerror or exc}") from exc
    return sock, int(sock.getsockname()[1])


def _make_server(app: Any, settings: Any, port: int) -> Any:
    """Build a uvicorn server for ``app``, to serve the socket from :func:`_listen`."""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=port,
        log_level="warning",  # the tool owns the terminal; stay quiet
        ws="none",  # a WebSocket upgrade arrives as a plain HTTP request
        log_config=None,  # uvicorn's records go to Privyx's handlers, not stderr
    )
    return uvicorn.Server(config)


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


def _set_terminal_title(name: str) -> None:
    """Give terminals a title when the child does not set one of its own."""
    stream = sys.stderr
    if not stream.isatty() or os.environ.get("TERM") == "dumb":
        return

    # Shell preexec hooks may have named the tab after privyx or uv. Set a
    # fallback once so the agent can replace it with its own dynamic title.
    # Terminals configured to show only the process name may ignore this.
    # Never put control characters in OSC data, or include arguments that might
    # contain secrets.
    title = "".join(char for char in Path(name).name if char.isprintable())
    stream.write(f"\033]0;{title}\007")
    stream.flush()


async def _spawn(plan: LaunchPlan) -> int:
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
