"""`privyx doctor` — run the diagnostic checks of the configuration."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

import click

from privyx.cli._options import config_options, json_option


@click.command()
@config_options
@json_option
def doctor(as_json: bool) -> None:
    """Run diagnostic checks system-wide.

    Each check exercises the configured component: the vault round-trips a
    session, the proxy masks a sample request, a stream restores a pseudonym
    split across chunks.  Exits 1 when a check fails.
    """
    from privyx.app import Privyx

    checks = Privyx().diagnostics.doctor()
    if as_json:
        click.echo(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        click.echo("Privyx Doctor")
        click.echo("=============")
        for check in checks:
            click.echo(f"  {'✓' if check.ok else '✗'} {check.name}: {check.message}")
    if not all(check.ok for check in checks):
        sys.exit(1)
