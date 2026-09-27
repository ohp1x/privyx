"""CLI main entry point — orchestration layer.

Each sub-command lives in its own module under ``privyx.cli.commands``.
This module only wires them together.

Usage::

    privyx proxy [--config FILE] [--port PORT] [--host HOST] [--upstream URL]
    privyx run [--provider openai|anthropic|generic] [--config FILE]
    privyx detect [--stdin] TEXT
    privyx mask [--stdin | -i FILE] [--map FILE] [--session ID] TEXT
    privyx unmask [--stdin | -i FILE] [--map FILE] [--session ID] TEXT
    privyx session list|show|prune
    privyx inspect session <session_id>   (alias of `session show`)
    privyx doctor [--config FILE]
    privyx config [--show] [--path]
"""

from __future__ import annotations

import click

from privyx import __version__
from privyx.cli.commands.config import config_cmd
from privyx.cli.commands.detect import detect
from privyx.cli.commands.doctor import doctor
from privyx.cli.commands.inspect import inspect
from privyx.cli.commands.mask import mask, unmask
from privyx.cli.commands.proxy import proxy
from privyx.cli.commands.run import run
from privyx.cli.commands.session import session


@click.group()
@click.version_option(version=__version__, prog_name="privyx")
def cli() -> None:
    """Privyx — AI data privacy gateway."""


cli.add_command(proxy)
cli.add_command(run)
cli.add_command(detect)
cli.add_command(mask)
cli.add_command(unmask)
cli.add_command(inspect)
cli.add_command(session)
cli.add_command(doctor)
cli.add_command(config_cmd, name="config")


if __name__ == "__main__":
    cli()
