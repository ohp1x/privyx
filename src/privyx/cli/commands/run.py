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
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from click.core import ParameterSource

from privyx.cli._options import config_options, json_option

if TYPE_CHECKING:
    from privyx.agents import LaunchPlan
    from privyx.agents._plan import Target
    from privyx.app import Privyx
    from privyx.config.schema import Settings


def _run_settings(
    app: Privyx,
    provider: str | None,
    upstream: str | None,
    session_strategy: str | None,
    no_anchor: bool,
    *,
    harvest: bool | None = None,
    agent: str | None = None,
    sources: dict[str, str] | None = None,
) -> Settings:
    extra: dict[str, Any] = {}
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
    return app._load(app._config, extra=extra, base_extra=base_extra, agent=agent, sources=sources)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("target", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@config_options
@click.option(
    "--provider", "-p", default=None, help="Use this named provider (see `providers`)"
)
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
    help="No anchor secret unless the config sets one (default: auto).",
)
@click.option(
    "--harvest/--no-harvest",
    default=None,
    help="Also mask git authors, remotes, .env values, and the OS username (default: config).",
)
@click.option("--list", "list_targets", is_flag=True, help="List known targets and exit")
@click.option("--dry-run", is_flag=True, help="Show the launch plan without starting anything")
@json_option
def run(
    target: str | None,
    args: tuple[str, ...],
    provider: str | None,
    upstream: str | None,
    port: int,
    env_vars: tuple[str, ...],
    session_strategy: str | None,
    no_anchor: bool,
    harvest: bool | None,
    list_targets: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Run TARGET with its traffic routed through Privyx.

    Examples:

    \b
      privyx run claude
      privyx run codex --provider openai
      privyx run --env-var MY_TOOL_BASE_URL -- my-tool --flag
    """
    from privyx.agents import Agent
    from privyx.app import Privyx
    from privyx.app._run import can_exec_agent, exec_agent, expand, launch_target, plan_launch
    from privyx.core import ConfigError, PrivyxError

    if not target and not list_targets:
        raise click.UsageError("missing TARGET (try `privyx run --list`)")
    if as_json and not (list_targets or dry_run):
        raise click.UsageError("--json writes the plan of --dry-run or the list of --list")

    app = Privyx()
    sources: dict[str, str] = {}
    try:
        settings = _run_settings(
            app,
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
        targets = sorted(settings.agents.items())
        if as_json:
            listed_json = [
                {"name": name, "type": p.type, "provider": p.provider, "env_vars": p.env_vars}
                for name, p in targets
            ]
            click.echo(json.dumps(listed_json, indent=2))
            return
        click.echo("Known targets:")
        for name, listed in targets:
            click.echo(f"  {name:<8} provider={listed.provider:<10} {', '.join(listed.env_vars)}")
        return

    assert target is not None
    try:
        expanded = expand(app, target, args) if target not in settings.agents else None
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    if expanded is not None:
        target, args = expanded
        sources = {}
        try:
            settings = _run_settings(
                app,
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
    try:
        spec = launch_target(app, settings, target, env_vars=env_vars, sources=sources)
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    all_env_vars = tuple(spec.env_vars) + tuple(env_vars)
    # No variable and no argument of its type's points the tool at the proxy.
    if not all_env_vars and type(spec.agent).argv is Agent.argv:
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
        plan = plan_launch(
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
    if settings.harvest:
        from privyx.workspace import harvest as harvest_workspace

        harvested: str | None = harvest_workspace(settings, Path.cwd())
    else:
        harvested = None
    if dry_run:
        if as_json:
            click.echo(json.dumps(_plan_json(plan, settings, target, harvested), indent=2))
            return
        _show_plan(plan)
        _show_presets(settings, sources, target)
        if harvested is not None:
            click.echo(f"Harvest: {harvested}")
        return
    if harvested is not None:
        click.echo(f"Harvest: {harvested}", err=True)

    if shutil.which(plan.command, path=plan.environment.get("PATH")) is None:
        click.echo(f"Error: {spec.command!r} not found on PATH", err=True)
        sys.exit(127)
    try:
        spec.agent.check(plan.upstream)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    target_run = partial(
        _run_target,
        spec=spec,
        argv=list(args),
        upstream=upstream,
        port=port,
        env_vars=all_env_vars,
        session_strategy=session_strategy,
        no_anchor=no_anchor,
        settings=settings,
        plan=plan,
        app=app,
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


def _show_running(plan: LaunchPlan) -> None:
    from privyx.security import redact, redact_argv

    click.echo(f"Privyx proxy → {redact(plan.route, 'url')}", err=True)
    click.echo(f"Running: {shlex.join(redact_argv([plan.command, *plan.argv]))}", err=True)
    click.echo(f"  {', '.join(plan.env_vars)} = {plan.base_url}", err=True)
    _set_terminal_title(plan.profile)


def _show_plan(plan: LaunchPlan) -> None:
    from privyx.security import redact, redact_argv

    sources = plan.sources
    click.echo(f"Profile: {plan.profile}")
    click.echo(f"Command: {shlex.join(redact_argv([plan.command, *plan.argv]))}")
    click.echo(f"Type: {plan.target.type}")
    click.echo(f"Proxy: {plan.base_url} ({plan.proxy_mode})")
    click.echo(f"Provider: {plan.provider} [{sources.get('provider.type', 'defaults')}]")
    click.echo(f"Upstream: {redact(plan.upstream, 'upstream_url')} [{sources['run.upstream']}]")
    if plan.native_endpoint:
        endpoint = plan.native_endpoint
        action = "used" if sources["run.upstream"] == endpoint.origin else "overridden"
        click.echo(f"Native endpoint ({action}): {redact(endpoint.url, 'url')} [{endpoint.origin}]")
    click.echo(f"Routing: tool → {plan.base_url} → {redact(plan.route, 'upstream_url')}")
    click.echo(f"Session: {plan.session} [{sources.get('session.strategy', 'defaults')}]")
    click.echo(f"Anchor: {plan.anchor} [{sources.get('anchor.secret', 'defaults')}]")
    budget = plan.target.budget
    limits = [f"{budget.max_turns} turns"] if budget.max_turns else []
    limits += [f"{budget.max_tokens} tokens"] if budget.max_tokens else []
    click.echo(f"Budget: {', '.join(limits) or 'none'}")
    prefix = f"agents.{plan.profile}."
    for field_name in ("command", "base_path", "type", "args"):
        fallback = "CLI" if field_name == "command" else "defaults"
        click.echo(f"Source {field_name}: {sources.get(prefix + field_name, fallback)}")
    for path in ("server.host", "proxy.mode", "run.port"):
        click.echo(f"Source {path}: {sources.get(path, 'defaults')}")
    click.echo("Source arguments: CLI")
    click.echo("Environment injected:")
    for key in sorted(plan.injected_env):
        value = plan.base_url if key in plan.env_vars else "***"
        click.echo(f"  {key}={value} [{plan.env_sources[key]}]")


def _plan_json(
    plan: LaunchPlan, settings: Settings, profile: str, harvested: str | None
) -> dict[str, Any]:
    """What ``--dry-run`` shows, as data: no credential, and URLs with their secrets masked."""
    from privyx.config import CHOSEN_PRESETS
    from privyx.security import redact, redact_argv

    sources = plan.sources
    budget = plan.target.budget
    endpoint = plan.native_endpoint
    listed = settings.agents.get(profile)
    presets = [*(listed.presets if listed else []), *CHOSEN_PRESETS.get()]
    return {
        "profile": plan.profile,
        "type": plan.target.type,
        "command": plan.command,
        "argv": redact_argv([plan.command, *plan.argv])[1:],
        "proxy": {"url": plan.base_url, "mode": plan.proxy_mode},
        "provider": {"name": plan.provider, "source": sources.get("provider.type", "defaults")},
        "upstream": {
            "url": redact(plan.upstream, "upstream_url"),
            "source": sources["run.upstream"],
        },
        "native_endpoint": None
        if endpoint is None
        else {"url": redact(endpoint.url, "url"), "source": endpoint.origin},
        "route": redact(plan.route, "upstream_url"),
        "session": {
            "strategy": plan.session,
            "source": sources.get("session.strategy", "defaults"),
        },
        "anchor": {"value": plan.anchor, "source": sources.get("anchor.secret", "defaults")},
        "budget": {"max_turns": budget.max_turns, "max_tokens": budget.max_tokens},
        "presets": presets,
        "environment": {
            key: {
                "value": plan.base_url if key in plan.env_vars else "***",
                "source": plan.env_sources[key],
            }
            for key in sorted(plan.injected_env)
        },
        "harvest": harvested,
    }


def _show_presets(settings: Settings, sources: dict[str, str], profile: str) -> None:
    """Name the presets applied, then each setting they decide, its value masked as needed.

    A setting a later layer overrides is not one a preset decides, so it is
    left out.  A value that cannot be looked up by its dotted path (a key
    holding a dot) is left out, not guessed.
    """
    from privyx.config import CHOSEN_PRESETS
    from privyx.security import redact

    listed = settings.agents.get(profile)
    origin = sources.get(f"agents.{profile}.presets", "defaults")
    applied = [(name, origin) for name in (listed.presets if listed else [])]
    applied += [(name, "CLI") for name in CHOSEN_PRESETS.get()]
    if not applied:
        return
    click.echo("Presets: " + ", ".join(f"{name} [{source}]" for name, source in applied))
    layers = {f"presets.{name}" for name, _ in applied}
    decided = sorted(path for path, source in sources.items() if source in layers)
    values = redact(settings.model_dump(mode="json", exclude={"agents", "presets"}))
    for path in decided:
        if any(other.startswith(path + ".") for other in decided):
            continue  # a list of detectors, say: its items' settings are shown
        value: Any = values
        for part in path.split("."):
            if isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                break
        else:
            click.echo(f"  {path} = {json.dumps(value)} [{sources[path]}]")
            continue
        click.echo(f"  {path} [{sources[path]}]")


async def _run_target(
    spec: Target,
    argv: list[str],
    upstream: str | None,
    port: int,
    env_vars: tuple[str, ...],
    session_strategy: str | None = None,
    no_anchor: bool = False,
    settings: Settings | None = None,
    plan: LaunchPlan | None = None,
    ready: socket.socket | None = None,
    parent: int | None = None,
    *,
    app: Privyx | None = None,
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
    from privyx.app import Privyx
    from privyx.app._run import plan_launch, run_agent, wait_for_agent_exit
    from privyx.observability.logging import configure_logging

    app = app or Privyx()
    if settings is None:
        settings = _run_settings(app, spec.provider, upstream, session_strategy, no_anchor)
    if plan is None:
        plan = plan_launch(spec, argv, settings, port, env_vars)
    # The child tool owns the terminal: log lines go to log.file if one is set,
    # else nowhere, and the audit trail still records every error.
    configure_logging(settings, console=False)
    # Bound before the rest opens, so a port in use leaves nothing to undo.
    sock, bound_port = _listen(settings.server.host, port)

    async def launch(bound: LaunchPlan) -> int:
        # On stderr: stdout is the tool's, and may be piped into a parser.
        if ready is not None and parent is not None:
            ready.sendall(json.dumps({"port": bound.port}).encode() + b"\n")
            ready.close()
            await wait_for_agent_exit(parent)
            return 0
        _show_running(bound)
        return await _spawn(bound)

    outcome = await run_agent(
        app,
        settings,
        plan,
        sock,
        bound_port,
        budget=spec.budget,
        launch=launch,
        # The worker handles SIGTERM itself; uvicorn must not re-raise it during cleanup.
        quiet_signals=parent is not None,
    )
    # After exec the shell has likely drawn its prompt already.
    lead = "\n" if parent is not None else ""
    counts = outcome.unproxied
    # Only content passed through unmasked, or a request blocked or failed,
    # shows something went wrong. No request at all is what help, a tool
    # closed before its first prompt, or a refused model look like too, so
    # that is left to the audit trail.
    if counts is not None and (outcome.unmasked or counts["blocked"] or counts["failed"]):
        click.echo(
            lead + f"Warning: {outcome.requests} request(s) from {spec.command} "
            "reached Privyx, but none was sent upstream on a masking route "
            f"({counts['passthrough']} passed through unmasked, "
            f"{counts['blocked']} blocked, {counts['failed']} failed).",
            err=True,
        )
    if outcome.stopped is not None:
        reason, turns, tokens = outcome.stopped
        click.echo(
            lead + f"Privyx stopped {spec.command}: the {reason}, "
            f"after {turns} turn(s) and {tokens} tokens.",
            err=True,
        )
    return outcome.code


def _listen(host: str, port: int) -> tuple[socket.socket, int]:
    """Bind the proxy's socket on ``port`` (0 picks a free one); return it and its port.

    Raises:
        PrivyxError: If the port cannot be bound, e.g. another process uses it.
    """
    from privyx.core import PrivyxError

    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn's own bind does
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise PrivyxError(f"cannot listen on {host}:{port}: {exc.strerror or exc}") from exc
    return sock, int(sock.getsockname()[1])


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
