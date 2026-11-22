"""`privyx plugin` — the plugins the config loads, and the ones installed."""

from __future__ import annotations

import json
import sys

import click

from privyx.cli._options import config_options, json_option


@click.group()
def plugin() -> None:
    """List the plugins loaded and installed."""


@plugin.command("list")
@config_options
@json_option
def list_plugins(as_json: bool) -> None:
    """List the plugins the config loads, what each adds, and the ones installed.

    An installed package is listed from its `privyx.plugins` entry point, and
    never loaded unless the config names it in `plugins.modules`.
    """
    from dataclasses import asdict

    from privyx.app import Privyx
    from privyx.core import PrivyxError

    try:
        found = Privyx().plugins.list()
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(asdict(found), indent=2))
        return
    loaded = ", ".join(f"{name} ({where})" for where, name in found.loaded)
    click.echo(f"plugins: {loaded or 'none loaded'}")
    for added in found.components:
        extends = f"   extends {added.extends}" if added.extends else ""
        click.echo(f"  {added.kind:<9} {added.name:<15} {added.source}{extends}")
    for name, value in found.installed:
        if not any(value.split(":")[0] == loaded_name for _, loaded_name in found.loaded):
            click.echo(f"installed, not loaded: {name} ({value})")
