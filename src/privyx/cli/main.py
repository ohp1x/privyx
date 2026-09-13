"""CLI main entry point — orchestration layer.

Each sub-command lives in its own module under ``privyx.cli.commands``.
This module only wires them together.

Usage::

    privyx proxy [--config FILE] [--port PORT] [--host HOST] [--upstream URL]
    privyx run [--provider openai|anthropic|generic] [--config FILE]
    privyx detect [--stdin] TEXT
    privyx inspect session <session_id>
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
from privyx.cli.commands.proxy import proxy
from privyx.cli.commands.run import run
from privyx.cli.commands.session import session  # noqa: F401 — registers under inspect


@click.group()
@click.version_option(version=__version__, prog_name="privyx")
def cli() -> None:
    """Privyx — AI data privacy gateway."""


cli.add_command(proxy)
cli.add_command(run)
cli.add_command(detect)
cli.add_command(inspect)
cli.add_command(doctor)
cli.add_command(config_cmd, name="config")


if __name__ == "__main__":
    cli()
