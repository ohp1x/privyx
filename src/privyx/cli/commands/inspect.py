"""`privyx inspect` command group — inspect internal state."""

from __future__ import annotations

import click


@click.group("inspect")
def inspect() -> None:
    """Inspect Privyx internal state."""
