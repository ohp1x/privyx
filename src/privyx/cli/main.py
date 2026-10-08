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
    privyx audit stats|tail [FILE]
    privyx doctor [--config FILE]
    privyx config [--show] [--path]
    privyx trust
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import click

from privyx import __version__
from privyx.cli.commands.audit import audit
from privyx.cli.commands.config import config_cmd
from privyx.cli.commands.detect import detect
from privyx.cli.commands.doctor import doctor
from privyx.cli.commands.inspect import inspect
from privyx.cli.commands.mask import mask, unmask
from privyx.cli.commands.proxy import proxy
from privyx.cli.commands.run import run
from privyx.cli.commands.session import session
from privyx.cli.commands.trust import trust


@click.group()
@click.version_option(version=__version__, prog_name="privyx")
def cli() -> None:
    """Privyx — AI data privacy gateway."""
    _adopt_earlier_files()


def _adopt_earlier_files() -> None:
    """Move what earlier versions kept outside ``~/.privyx`` into it.

    The anchor secret, so pseudonyms stay what they were, and the audit trail
    of ``privyx run``.  A file ``~/.privyx`` already has is never replaced.
    """
    home = os.path.expanduser("~")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    for earlier, name in (
        (Path(config, "privyx", "anchor.key"), "anchor.key"),
        (Path(state, "privyx", "audit.log"), "audit.log"),
    ):
        target = Path(home, ".privyx", name)
        if not earlier.is_file() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(earlier, target)
        except OSError:
            continue  # stays where it was
        click.echo(f"Moved {earlier} to {target}", err=True)


cli.add_command(proxy)
cli.add_command(run)
cli.add_command(detect)
cli.add_command(mask)
cli.add_command(unmask)
cli.add_command(inspect)
cli.add_command(session)
cli.add_command(audit)
cli.add_command(doctor)
cli.add_command(config_cmd, name="config")
cli.add_command(trust)


if __name__ == "__main__":
    cli()
