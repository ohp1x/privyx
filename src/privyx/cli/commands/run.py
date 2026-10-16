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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from click.core import ParameterSource

from privyx.config.defaults import DEFAULT_AGENTS
from privyx.config.schema import AgentConfig

if TYPE_CHECKING:
    from privyx.config.schema import Settings

_AUTO_ANCHOR = "__privyx_auto_anchor__"


@dataclass(frozen=True)
class Target:
    """A downstream tool and how to point it at a local proxy.

    Attributes:
        command: Executable to launch.
        provider: Provider type the tool expects upstream.
        env_vars: Environment variables to set to the proxy's base URL.
        base_path: Appended to the proxy's URL for a tool whose base URL ends
            in the API version, as the OpenAI SDK's does (``…/v1``).
        args: Builds the tool's arguments from that URL and the user's own,
            for a tool whose own config outranks its environment.
        extra_env: Fixed environment variables the tool needs (e.g. a dummy key
            when the tool refuses to start without one — the real key is added
            upstream by the proxy).
    """

    command: str
    provider: str
    env_vars: tuple[str, ...]
    base_path: str = ""
    args: Callable[[str, list[str]], list[str]] | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


def _claude_args(base_url: str, argv: list[str]) -> list[str]:
    # A base URL in Claude Code's settings.json outranks its environment, and
    # --settings outranks settings.json.
    return ["--settings", json.dumps({"env": {"ANTHROPIC_BASE_URL": base_url}}), *argv]


def _codex_args(base_url: str, argv: list[str]) -> list[str]:
    # Codex reads its base URL from its config, not from OPENAI_BASE_URL.
    override = ["-c", f"openai_base_url={json.dumps(base_url)}"]
    # A -c after a subcommand replaces every -c before it, this one included:
    # when the user's arguments hold one, the override is said again after
    # them.  Past a `--` it would be read as an argument.
    end = argv.index("--") if "--" in argv else len(argv)
    own = any(arg.startswith(("-c", "--config")) for arg in argv[:end])
    return [*override, *argv[:end], *(override if own else []), *argv[end:]]


def _profile_target(name: str, profile: AgentConfig) -> Target:
    # Keep the existing CLI overrides for built-ins and aliases of their commands.
    adapter = name if name in {"claude", "codex"} else Path(profile.command).name
    return Target(
        command=profile.command,
        provider=profile.provider,
        env_vars=tuple(profile.env_vars),
        base_path=profile.base_path,
        args={"claude": _claude_args, "codex": _codex_args}.get(adapter),
    )


TARGETS: dict[str, Target] = {
    name: _profile_target(name, AgentConfig.model_validate(profile))
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
        base_extra["anchor"] = {"secret": _AUTO_ANCHOR}
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
        _profile_target(target, profile)
        if profile
        else Target(command=target, provider=settings.provider.type, env_vars=())
    )
    spec = replace(spec, provider=settings.provider.type)
    all_env_vars = tuple(spec.env_vars) + tuple(env_vars)
    if not all_env_vars:
        raise click.UsageError(
            f"don't know how to point {target!r} at the proxy; "
            "pass --env-var NAME (see `privyx run --list`)"
        )

    if dry_run:
        sources["run.port"] = (
            "CLI"
            if click.get_current_context().get_parameter_source("port")
            == ParameterSource.COMMANDLINE
            else "run defaults"
        )
        _show_plan(target, spec, list(args), settings, port, all_env_vars, env_vars, sources)
        return

    if shutil.which(spec.command) is None:
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
            )
        )
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    sys.exit(code)


def _base_url(host: str, port: int | str, base_path: str) -> str:
    host = f"[{host}]" if ":" in host else host
    return f"http://{host}:{port}{base_path}"


def _show_plan(
    name: str,
    spec: Target,
    argv: list[str],
    settings: Settings,
    port: int,
    env_vars: tuple[str, ...],
    extra_env_vars: tuple[str, ...],
    sources: dict[str, str],
) -> None:
    from privyx.config.redact import redact, redact_argv
    from privyx.providers.registry import resolve_base_url, resolve_origin

    base_url = _base_url(settings.host, port or "<auto>", spec.base_path)
    args = spec.args(base_url, argv) if spec.args else argv
    upstream = resolve_base_url(settings)
    route = upstream if settings.proxy.mode == "gateway" else resolve_origin(settings)
    upstream_key = "upstream_url" if settings.upstream_url else "provider.base_url"
    upstream_source = (
        sources.get(upstream_key, "defaults")
        if (settings.upstream_url or settings.provider.base_url)
        else f"provider default ({sources.get('provider.type', 'defaults')})"
    )
    click.echo(f"Profile: {name}")
    click.echo(f"Command: {shlex.join(redact_argv([spec.command, *args]))}")
    click.echo(f"Proxy: {base_url} ({settings.proxy.mode})")
    click.echo(f"Provider: {settings.provider.type} [{sources.get('provider.type', 'defaults')}]")
    click.echo(f"Upstream: {redact(upstream, 'upstream_url')} [{upstream_source}]")
    click.echo(f"Routing: tool → {base_url} → {redact(route, 'upstream_url')}")
    click.echo(
        f"Session: {settings.session.strategy} [{sources.get('session.strategy', 'defaults')}]"
    )
    anchor = (
        "auto-provision on launch"
        if settings.anchor.secret == _AUTO_ANCHOR
        else ("configured (masked)" if settings.anchor.secret else "disabled")
    )
    click.echo(f"Anchor: {anchor} [{sources.get('anchor.secret', 'defaults')}]")
    prefix = f"agents.{name}."
    for field_name in ("command", "base_path"):
        fallback = "CLI" if field_name == "command" and name not in settings.agents else "defaults"
        click.echo(f"Source {field_name}: {sources.get(prefix + field_name, fallback)}")
    for path in ("host", "proxy.mode", "run.port"):
        click.echo(f"Source {path}: {sources.get(path, 'defaults')}")
    click.echo("Source arguments: CLI")
    click.echo("Environment injected:")
    for env_name in dict.fromkeys(env_vars):
        source = (
            "CLI" if env_name in extra_env_vars else sources.get(prefix + "env_vars", "defaults")
        )
        click.echo(f"  {env_name}={base_url} [{source}]")


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
    if settings.anchor.secret == _AUTO_ANCHOR:
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
        base_url = _base_url(settings.host, bound_port, spec.base_path)
        argv = spec.args(base_url, argv) if spec.args else argv

        # On stderr: stdout is the tool's, and may be piped into a parser.
        click.echo(f"Privyx proxy → {redact(target, 'url')}", err=True)
        click.echo(f"Running: {shlex.join(redact_argv([spec.command, *argv]))}", err=True)
        click.echo(f"  {', '.join(env_vars)} = {base_url}", err=True)

        code = await _spawn(spec, argv, base_url, env_vars)
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


async def _spawn(spec: Target, argv: list[str], base_url: str, env_vars: tuple[str, ...]) -> int:
    """Run the tool as a child process with the proxy URL injected.

    Signals are left to the child: it owns the terminal, so Ctrl-C reaches it
    directly and this process just waits for the exit code.
    """
    env = dict(os.environ)
    env.update(spec.extra_env)
    for name in env_vars:
        env[name] = base_url

    process = await asyncio.create_subprocess_exec(spec.command, *argv, env=env)
    try:
        return await process.wait()
    except asyncio.CancelledError:  # pragma: no cover - interactive only
        process.terminate()
        await process.wait()
        raise
