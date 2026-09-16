"""`privyx run` command — run a downstream tool through the Privyx proxy.

``privyx run claude`` wires::

    Claude Code  →  Privyx  →  Anthropic

by starting the proxy on a local port and launching the tool with its base-URL
environment variable pointed at that port.  The tool is unmodified and unaware;
it just talks to what it thinks is the provider.

This is orchestration only (principle #12): no privacy logic lives here.  The
engine and provider both come from the same builders ``privyx proxy``
uses, so both commands behave identically.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

import click


@dataclass(frozen=True)
class Target:
    """A downstream tool and how to point it at a local proxy.

    Attributes:
        command: Executable to launch.
        provider: Provider type the tool expects upstream.
        env_vars: Environment variables to set to the proxy's base URL.
        extra_env: Fixed environment variables the tool needs (e.g. a dummy key
            when the tool refuses to start without one — the real key is added
            upstream by the proxy).
    """

    command: str
    provider: str
    env_vars: tuple[str, ...]
    extra_env: dict[str, str] = field(default_factory=dict)


TARGETS: dict[str, Target] = {
    "claude": Target(
        command="claude",
        provider="anthropic",
        env_vars=("ANTHROPIC_BASE_URL",),
    ),
    "codex": Target(
        command="codex",
        provider="openai",
        env_vars=("OPENAI_BASE_URL",),
    ),
    "openai": Target(
        command="openai",
        provider="openai",
        env_vars=("OPENAI_BASE_URL",),
    ),
    "aider": Target(
        command="aider",
        provider="openai",
        env_vars=("OPENAI_API_BASE", "OPENAI_BASE_URL"),
    ),
}


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("target", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.option("--provider", "-p", default=None, help="Override the upstream provider type")
@click.option("--upstream", "-u", default=None, help="Override the upstream URL")
@click.option("--port", default=0, type=int, help="Proxy port (0 = pick a free one)")
@click.option(
    "--env-var",
    "env_vars",
    multiple=True,
    help="Extra environment variable to set to the proxy URL (repeatable)",
)
@click.option("--list", "list_targets", is_flag=True, help="List known targets and exit")
def run(
    target: str | None,
    args: tuple[str, ...],
    config_path: str | None,
    provider: str | None,
    upstream: str | None,
    port: int,
    env_vars: tuple[str, ...],
    list_targets: bool,
) -> None:
    """Run TARGET with its traffic routed through Privyx.

    Examples:

    \b
      privyx run claude
      privyx run codex --provider openai
      privyx run --env-var MY_TOOL_BASE_URL -- my-tool --flag
    """
    from privyx.core.errors import PrivyxError

    if list_targets:
        click.echo("Known targets:")
        for name, spec in sorted(TARGETS.items()):
            click.echo(f"  {name:<8} provider={spec.provider:<10} {', '.join(spec.env_vars)}")
        return

    if not target:
        raise click.UsageError("missing TARGET (try `privyx run --list`)")

    spec = TARGETS.get(target) or Target(
        command=target, provider=provider or "generic", env_vars=()
    )
    if provider:
        spec = Target(
            command=spec.command,
            provider=provider,
            env_vars=spec.env_vars,
            extra_env=spec.extra_env,
        )
    all_env_vars = tuple(spec.env_vars) + tuple(env_vars)
    if not all_env_vars:
        raise click.UsageError(
            f"don't know how to point {target!r} at the proxy; "
            "pass --env-var NAME (see `privyx run --list`)"
        )

    if shutil.which(spec.command) is None:
        click.echo(f"Error: {spec.command!r} not found on PATH", err=True)
        sys.exit(127)

    import importlib.util

    if importlib.util.find_spec("uvicorn") is None:  # pragma: no cover
        click.echo(
            "`privyx run` requires privyx[server]. Install with: pip install privyx[server]",
            err=True,
        )
        sys.exit(1)

    try:
        code = asyncio.run(
            _run_target(
                spec=spec,
                argv=list(args),
                config_path=config_path,
                upstream=upstream,
                port=port,
                env_vars=all_env_vars,
            )
        )
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    sys.exit(code)


async def _run_target(
    spec: Target,
    argv: list[str],
    config_path: str | None,
    upstream: str | None,
    port: int,
    env_vars: tuple[str, ...],
) -> int:
    """Start the proxy, run the tool against it, and tear everything down.

    Returns:
        The tool's exit code.
    """
    from privyx.config.loader import load_config
    from privyx.core.builder import build_audit_logger, build_engine
    from privyx.plugins.loader import load_plugins
    from privyx.providers.registry import build_provider, resolve_base_url

    extra: dict[str, Any] = {"provider": {"type": spec.provider}}
    if upstream is not None:
        extra["upstream_url"] = upstream
        extra["provider"]["base_url"] = upstream
    settings = load_config(config_path, extra=extra)

    # The child tool owns the terminal, so we do not configure application
    # logging here; the audit trail still records to its file.
    hooks = load_plugins(settings)
    audit = build_audit_logger(settings)
    engine, close_vault = await build_engine(settings, audit=audit)
    provider = build_provider(settings)
    await hooks.run_startup()

    server, bound_port = _make_server(engine, provider, settings, port, audit)
    serve_task = asyncio.create_task(server.serve())
    try:
        await _wait_until_started(server, serve_task)
        base_url = f"http://{settings.host}:{bound_port}"

        click.echo(f"Privyx proxy → {resolve_base_url(settings)}")
        click.echo(f"Running: {spec.command} {' '.join(argv)}".rstrip())
        click.echo(f"  {', '.join(env_vars)} = {base_url}")

        return await _spawn(spec, argv, base_url, env_vars)
    finally:
        server.should_exit = True
        await serve_task
        await hooks.run_shutdown()
        await close_vault()
        await provider.close()
        audit.close()


def _make_server(
    engine: Any, provider: Any, settings: Any, port: int, audit: Any
) -> tuple[Any, int]:
    """Build a uvicorn server bound to ``port`` (0 picks a free one)."""
    import socket

    import uvicorn

    from privyx.gateway.server import Gateway

    if port == 0:
        with socket.socket() as sock:
            sock.bind((settings.host, 0))
            port = int(sock.getsockname()[1])

    gateway = Gateway(engine=engine, provider=provider, settings=settings, audit=audit)
    config = uvicorn.Config(
        gateway.app,
        host=settings.host,
        port=port,
        log_level="warning",  # the tool owns the terminal; stay quiet
    )
    return uvicorn.Server(config), port


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
