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
import shlex
import shutil
import socket
import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import click
from click.core import ParameterSource

from privyx.cli.launch import (
    AUTO_ANCHOR,
    LaunchPlan,
    Target,
    build_launch_plan,
    profile_target,
)
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
    profile = settings.agents.get(target)
    spec = (
        profile_target(target, profile)
        if profile
        else Target(command=target, provider=settings.provider.type, env_vars=())
    )
    spec = replace(spec, provider=settings.provider.type)
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
            "or, to launch a tool directly, pass --env-var NAME "
            "(see `privyx run --list`)"
        )

    sources["run.port"] = (
        "CLI"
        if click.get_current_context().get_parameter_source("port") == ParameterSource.COMMANDLINE
        else "run defaults"
    )
    try:
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
    if dry_run:
        _show_plan(plan)
        return

    if shutil.which(plan.command, path=plan.environment.get("PATH")) is None:
        click.echo(f"Error: {spec.command!r} not found on PATH", err=True)
        sys.exit(127)

    try:
        code = asyncio.run(
            _run_target(
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
        )
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    sys.exit(code)


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
    from privyx.cli.commands.proxy import build_transparent_proxy
    from privyx.config.redact import redact, redact_argv
    from privyx.core.builder import build_audit_logger, build_engine
    from privyx.gateway.server import Gateway
    from privyx.gateway.transparent import create_transparent_app
    from privyx.observability.logging import configure_logging
    from privyx.plugins.loader import load_plugins
    from privyx.providers.registry import build_provider, resolve_base_url, resolve_origin
    from privyx.security.keys import read_or_create_anchor_secret

    if settings is None:
        settings = _run_settings(config_path, spec.provider, upstream, session_strategy, no_anchor)
    if plan is None:
        plan = build_launch_plan(spec, argv, settings, port, env_vars)
    if settings.anchor.secret == AUTO_ANCHOR:
        settings.anchor.secret = read_or_create_anchor_secret()
    # The child tool owns the terminal: log lines go to log_file if one is set,
    # else nowhere, and the audit trail still records every error.
    configure_logging(settings, console=False)
    # Bound before the rest opens, so a port in use leaves nothing to undo.
    sock, bound_port = _listen(settings.host, port)

    hooks = load_plugins(settings)
    audit = build_audit_logger(settings)
    engine, close_vault = await build_engine(settings, audit=audit)
    if settings.proxy.mode == "gateway":
        provider = build_provider(settings)
        app = Gateway(engine=engine, provider=provider, settings=settings, audit=audit).app
        close_upstream, target = provider.close, resolve_base_url(settings)
    else:
        proxy = build_transparent_proxy(settings, engine, audit)
        app = create_transparent_app(engine, proxy, settings)
        close_upstream, target = proxy.close, resolve_origin(settings)
    await hooks.run_startup()

    counted = _Counted(app)
    server = _make_server(counted, settings, bound_port)
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _wait_until_started(server, serve_task)
        plan = plan.bind_port(bound_port)

        # On stderr: stdout is the tool's, and may be piped into a parser.
        click.echo(f"Privyx proxy → {redact(target, 'url')}", err=True)
        click.echo(f"Running: {shlex.join(redact_argv([plan.command, *plan.argv]))}", err=True)
        click.echo(f"  {', '.join(plan.env_vars)} = {plan.base_url}", err=True)

        code = await _spawn(plan)
        if not counted.requests:
            # What a tool that ignores the proxy URL looks like from here.
            click.echo(
                f"Warning: no request from {spec.command} reached Privyx; if it called "
                "its provider, it did so directly, without masking.",
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
