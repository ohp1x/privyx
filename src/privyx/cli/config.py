"""CLI configuration helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click


def config_options[F: Callable[..., Any]](fn: F) -> F:
    """Add ``--config``/``-c`` (as ``config_path``) and ``--preset NAME`` (repeatable).

    The presets go to :data:`privyx.config.loader.CHOSEN_PRESETS`, which every
    ``load_config`` the command makes reads, and are cleared when it ends.
    """

    def choose(ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]) -> None:
        from privyx.config.loader import CHOSEN_PRESETS

        if value:
            token = CHOSEN_PRESETS.set(value)
            ctx.call_on_close(lambda: CHOSEN_PRESETS.reset(token))

    fn = click.option(
        "--preset",
        multiple=True,
        expose_value=False,
        callback=choose,
        help="Apply the settings of this preset under `presets` (repeatable; later ones win).",
    )(fn)
    return click.option("--config", "-c", "config_path", default=None, help="Config file path")(fn)
