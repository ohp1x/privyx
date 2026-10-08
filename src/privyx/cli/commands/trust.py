"""`privyx trust` command — allow the working directory's config files."""

from __future__ import annotations

import sys

import click


@click.command()
def trust() -> None:
    """Trust this directory's privyx.yaml and .privyx/config.yaml.

    A config file found in the working directory can load plugins and choose
    the upstream, so Privyx reads one only after you trust it here, and again
    after each change to it.
    """
    from privyx.config.loader import find_local_configs
    from privyx.config.loader import trust as trust_config
    from privyx.core.errors import PrivyxError

    paths = find_local_configs()
    if not paths:
        click.echo("Error: no privyx.yaml or .privyx/config.yaml to trust here", err=True)
        sys.exit(1)
    try:
        for path in paths:
            trust_config(path)
            click.echo(f"Trusted {path}")
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
