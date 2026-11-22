"""Options, messages, and stdio handling the commands share."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import click


def use_config(ctx: click.Context, path: str) -> None:
    """Read config file ``path`` alone in the command ``ctx`` runs, as ``-c`` does.

    It is passed on as ``PRIVYX_CONFIG``, restored when the command ends.
    """
    previous = os.environ.get("PRIVYX_CONFIG")

    def restore() -> None:
        if previous is None:
            os.environ.pop("PRIVYX_CONFIG", None)
        else:
            os.environ["PRIVYX_CONFIG"] = previous

    ctx.call_on_close(restore)
    # Keep symlinks intact: relative includes use the given config's parent.
    os.environ["PRIVYX_CONFIG"] = str(Path(path).absolute())


def choose_presets(ctx: click.Context, presets: tuple[str, ...]) -> None:
    """Apply ``presets`` in every config the command ``ctx`` runs reads, as ``--preset`` does."""
    from privyx.config import CHOSEN_PRESETS

    token = CHOSEN_PRESETS.set(presets)
    ctx.call_on_close(lambda: CHOSEN_PRESETS.reset(token))


def deprecated(old: str, new: str) -> None:
    """Say on stderr that ``old`` is an earlier name of ``new``, kept until 0.4.0."""
    click.echo(f"Warning: {old} is deprecated; use {new} (removed in 0.4.0)", err=True)


def config_options[F: Callable[..., Any]](fn: F) -> F:
    """The ``-c`` and ``--preset`` each command took before they were global.

    Hidden, and read with a warning: write them before the command,
    ``privyx -c FILE COMMAND``.
    """

    def config(ctx: click.Context, _param: click.Parameter, value: str | None) -> None:
        if value is not None:
            deprecated(f"`-c` after `{ctx.command_path}`", "`privyx -c FILE ...`")
            use_config(ctx, value)

    def preset(ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]) -> None:
        if value:
            deprecated(f"`--preset` after `{ctx.command_path}`", "`privyx --preset NAME ...`")
            choose_presets(ctx, value)

    fn = click.option("--preset", multiple=True, hidden=True, expose_value=False, callback=preset)(
        fn
    )
    return click.option(
        "--config", "-c", hidden=True, expose_value=False, callback=config, is_eager=True
    )(fn)


def json_option[F: Callable[..., Any]](fn: F) -> F:
    """``--json``: write the data as JSON, for a program to read."""
    return click.option("--json", "as_json", is_flag=True, help="Write the result as JSON.")(fn)


def interrupt(signum: int, frame: Any) -> NoReturn:
    """A SIGINT handler that raises at once, even inside a blocking read or write."""
    raise KeyboardInterrupt


def drop_stdout() -> None:
    """Send what is left in stdout's buffer nowhere.

    Python flushes it at exit, which would fail again on a closed pipe, or
    block on a full one that nobody reads.
    """
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
