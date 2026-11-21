"""`privyx features` command — the features, which are on, and what they listen to."""

from __future__ import annotations

import sys

import click

from privyx.cli.config import config_options


@click.group()
def features() -> None:
    """Show the optional features: audit trail, metrics, tracing, directive, ..."""


@features.command("list")
@click.option("--events", is_flag=True, help="Also show the events each one listens to.")
@config_options
def list_features(events: bool, config_path: str | None) -> None:
    """List each feature, and whether the config switches it on."""
    from privyx.app import Privyx
    from privyx.core import PrivyxError

    app = Privyx(config_path)
    try:
        found = app.features.list()
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    width = max(len(info.name) for info in found)
    for info in found:
        extends = f"  extends {info.extends}" if info.extends else ""
        click.echo(f"{info.name:<{width}}  {'on' if info.enabled else 'off'}{extends}")
        if events:
            for event, priority in info.listens:
                click.echo(f"  {event} (priority {priority})")
