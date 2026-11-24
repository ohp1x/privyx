"""`privyx features` — the features, which are on, what they listen to, and switching them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from privyx.cli._options import config_options, fail, json_option


@click.group()
def features() -> None:
    """List the optional features, and switch them on or off.

    audit, metrics, tracing, directive, cache_control, budget, a plugin's.
    """


@features.command("list")
@click.option("--events", is_flag=True, help="Also show the events each one listens to.")
@config_options
@json_option
def list_features(events: bool, as_json: bool) -> None:
    """List each feature, and whether the config switches it on."""
    from dataclasses import asdict

    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        found = Privyx().features.list()
    except PrivyxError as exc:
        fail(exc)
    if as_json:
        click.echo(json.dumps([asdict(info) for info in found], indent=2))
        return
    width = max(len(info.name) for info in found)
    for info in found:
        extends = f"  extends {info.extends}" if info.extends else ""
        click.echo(f"{info.name:<{width}}  {'on' if info.enabled else 'off'}{extends}")
        if events:
            for event, priority in info.listens:
                click.echo(f"  {event} (priority {priority})")


_PROJECT = click.option(
    "--project", is_flag=True, help="Write .privyx/settings.yaml here, not ~/.privyx/settings.yaml."
)
_FILE = click.option(
    "--file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write this file instead (needed with --config).",
)


@features.command("enable")
@click.argument("name")
@_PROJECT
@_FILE
def enable(name: str, project: bool, file: Path | None) -> None:
    """Switch feature NAME on, from the next start."""
    _switch(name, True, project, file)


@features.command("disable")
@click.argument("name")
@_PROJECT
@_FILE
def disable(name: str, project: bool, file: Path | None) -> None:
    """Switch feature NAME off, from the next start."""
    _switch(name, False, project, file)


def _switch(name: str, on: bool, project: bool, file: Path | None) -> None:
    import difflib

    from privyx.app import Privyx
    from privyx.core import ConfigError, PrivyxError

    app = Privyx()
    scope: Any = "project" if project else "user"
    try:
        known = [info.name for info in app.features.list()]
        if name not in known:
            match = difflib.get_close_matches(name, known, n=1)
            hint = f" (did you mean {match[0]!r}?)" if match else ""
            raise ConfigError(f"no feature {name!r}{hint}; `privyx features list` names them")
        changed = app.config.set(f"features.{name}.enabled", on, scope=scope, file=file)
    except PrivyxError as exc:
        fail(exc)
    click.echo(f"{'Enabled' if on else 'Disabled'} {name} in {changed.file}")
    for where in changed.above:
        click.echo(f"  {where} also writes {changed.key}, and its value is used")
