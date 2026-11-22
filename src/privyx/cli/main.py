"""The ``privyx`` command: its global options, and where each subcommand lives.

Each subcommand lives in its own module under ``privyx.cli.commands`` and is
imported only when it runs: ``privyx --help`` lists them from
:data:`COMMANDS`, which holds the line it shows for each, so it imports none
of them, nor the libraries they use.
"""

from __future__ import annotations

import importlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from privyx.version import __version__

#: Each command, in the order ``privyx --help`` lists them: where it is, and
#: its line there.
COMMANDS: dict[str, tuple[str, str]] = {
    "run": ("privyx.cli.commands.run:run", "Run TARGET with its traffic routed through Privyx."),
    "proxy": ("privyx.cli.commands.proxy:proxy", "Start the privacy proxy server."),
    "mask": ("privyx.cli.commands.mask:mask", "Replace sensitive values with tokens, reversibly."),
    "unmask": (
        "privyx.cli.commands.mask:unmask",
        "Put the original values back, from a --map file or a --session id.",
    ),
    "detect": (
        "privyx.cli.commands.detect:detect",
        "Detect sensitive entities in text, or check a regex.",
    ),
    "scan": (
        "privyx.cli.commands.scan:scan",
        "Find names and secrets in a workspace for Privyx to mask.",
    ),
    "doctor": ("privyx.cli.commands.doctor:doctor", "Run diagnostic checks system-wide."),
    "config": (
        "privyx.cli.commands.config:config",
        "Show, change, check, and migrate the configuration.",
    ),
    "session": ("privyx.cli.commands.session:session", "List, show, and prune vault sessions."),
    "audit": ("privyx.cli.commands.audit:audit", "Summarize and follow the audit trail."),
    "hook": (
        "privyx.cli.commands.hook:hook",
        "Git hooks that stop a commit holding a secret or a Privyx placeholder.",
    ),
    "mcp": ("privyx.cli.commands.mcp:mcp", "Use Privyx with MCP servers (stdio)."),
    "features": (
        "privyx.cli.commands.features:features",
        "List the optional features, and switch them on or off.",
    ),
    "plugin": ("privyx.cli.commands.plugin:plugin", "List the plugins loaded and installed."),
}

#: The names of earlier versions, hidden from ``privyx --help``: each runs
#: with a warning that names its replacement, until 0.4.0.
ALIASES: dict[str, str] = {
    "pipe": "privyx.cli.commands.mask:pipe",
    "trust": "privyx.cli.commands.config:trust_alias",
    "lint": "privyx.cli.commands.config:lint_alias",
    "inspect": "privyx.cli.commands.inspect:inspect",
    "install-hook": "privyx.cli.commands.hook:install_hook",
}


class LazyGroup(click.Group):
    """A group whose commands are imported when one runs, not when they are listed."""

    def __init__(
        self,
        *args: Any,
        lazy: Mapping[str, tuple[str, str]],
        hidden: Mapping[str, str],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.lazy = lazy
        self.aliases = hidden

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.lazy)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        where = self.lazy[cmd_name][0] if cmd_name in self.lazy else self.aliases.get(cmd_name)
        if where is None:
            return None
        module, _, name = where.partition(":")
        command: click.Command = getattr(importlib.import_module(module), name)
        return command

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        with formatter.section("Commands"):
            formatter.write_dl([(name, line) for name, (_, line) in self.lazy.items()])


@click.group(cls=LazyGroup, lazy=COMMANDS, hidden=ALIASES)
@click.version_option(version=__version__, prog_name="privyx")
@click.option("--config", "-c", "config_path", default=None, help="Read this config file alone.")
@click.option(
    "--preset",
    "presets",
    multiple=True,
    help="Apply the settings of this preset under `presets` (repeatable; later ones win).",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, presets: tuple[str, ...]) -> None:
    """Privyx — AI data privacy gateway."""
    from privyx.cli._options import choose_presets, use_config

    if config_path is not None:
        use_config(ctx, config_path)
    if presets:
        choose_presets(ctx, presets)
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


if __name__ == "__main__":
    cli()
