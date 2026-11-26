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

import json
import os
import shlex
import sys
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from click.core import ParameterSource

from privyx.cli._options import config_options, json_option

if TYPE_CHECKING:
    from privyx.agents import LaunchPlan
    from privyx.app._run import RunOutcome
    from privyx.config.schema import Settings


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
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    if not target and not list_targets:
        raise click.UsageError("missing TARGET (try `privyx run --list`)")
    if as_json and not (list_targets or dry_run):
        raise click.UsageError("--json writes the plan of --dry-run or the list of --list")

    app = Privyx()
    options: dict[str, Any] = {
        "provider": provider,
        "upstream": upstream,
        "session_strategy": session_strategy,
        "no_anchor": no_anchor,
        "harvest": harvest,
    }
    if list_targets:
        try:
            listed = app.agents.list(target, **options)
        except PrivyxError as exc:
            raise click.ClickException(str(exc)) from exc
        if as_json:
            click.echo(json.dumps([asdict(info) for info in listed], indent=2))
            return
        click.echo("Known targets:")
        for info in listed:
            click.echo(f"  {info.name:<8} provider={info.provider:<10} {', '.join(info.env_vars)}")
        return

    assert target is not None
    port_source = click.get_current_context().get_parameter_source("port")
    try:
        launch = app.agents.plan(
            target,
            args,
            port=port,
            port_given=port_source == ParameterSource.COMMANDLINE,
            env_vars=env_vars,
            **options,
        )
    except app.agents.BadTarget as exc:
        raise click.UsageError(str(exc)) from exc
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    harvested = app.agents.harvest(launch)
    if dry_run:
        if as_json:
            shown = _plan_json(launch.plan, launch.settings, launch.name, harvested)
            click.echo(json.dumps(shown, indent=2))
            return
        _show_plan(launch.plan)
        _show_presets(launch.settings, launch.sources, launch.name)
        if harvested is not None:
            click.echo(f"Harvest: {harvested}")
        return
    if harvested is not None:
        click.echo(f"Harvest: {harvested}", err=True)

    try:
        app.agents.check(launch)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(127 if isinstance(exc, app.agents.NotInstalled) else 1)

    try:
        code = app.agents.run(
            launch,
            announce=_show_running,
            report=partial(_show_outcome, command=launch.spec.command),
        )
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


def _show_outcome(outcome: RunOutcome, after_exec: bool, *, command: str) -> None:
    # After exec the shell has likely drawn its prompt already.
    lead = "\n" if after_exec else ""
    counts = outcome.unproxied
    # Only content passed through unmasked, or a request blocked or failed,
    # shows something went wrong. No request at all is what help, a tool
    # closed before its first prompt, or a refused model look like too, so
    # that is left to the audit trail.
    if counts is not None and (outcome.unmasked or counts["blocked"] or counts["failed"]):
        click.echo(
            lead + f"Warning: {outcome.requests} request(s) from {command} "
            "reached Privyx, but none was sent upstream on a masking route "
            f"({counts['passthrough']} passed through unmasked, "
            f"{counts['blocked']} blocked, {counts['failed']} failed).",
            err=True,
        )
    if outcome.stopped is not None:
        reason, turns, tokens = outcome.stopped
        click.echo(
            lead + f"Privyx stopped {command}: the {reason}, "
            f"after {turns} turn(s) and {tokens} tokens.",
            err=True,
        )


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
