"""`privyx inspect session` command — inspect a vault session."""

from __future__ import annotations

import asyncio
import sys

import click

from privyx.cli.commands.inspect import inspect


@inspect.command("session")
@click.argument("session_id")
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.option("--reveal", is_flag=True, help="Show original values in full (sensitive!)")
def session(session_id: str, config_path: str | None, reveal: bool) -> None:
    """Inspect a session vault entry.

    Original values are masked unless ``--reveal`` is given, so that routine
    debugging does not print PII to a terminal or CI log.
    """
    from privyx.core.errors import PrivyxError

    try:
        asyncio.run(_inspect_session(session_id, config_path, reveal))
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


async def _inspect_session(session_id: str, config_path: str | None, reveal: bool) -> None:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_vault

    settings = load_config(config_path)
    vault, close = await build_vault(settings)
    try:
        record = await vault.get(session_id)
    finally:
        await close()

    if record is None:
        click.echo(f"Session not found: {session_id}", err=True)
        click.echo(f"(vault: {settings.vault.type})", err=True)
        sys.exit(1)

    click.echo(f"Session: {record.session_id}")
    click.echo(f"  Vault:      {settings.vault.type}")
    click.echo(f"  Created:    {_ts(record.created_at)}")
    click.echo(f"  Updated:    {_ts(record.updated_at)}")
    click.echo(f"  Mappings:   {len(record.mapping)}")
    if record.metadata:
        click.echo(f"  Metadata:   {record.metadata}")
    if not record.mapping:
        return
    click.echo("")
    for pseudonym, original in record.mapping.items():
        shown = original if reveal else _mask(original)
        click.echo(f"  {pseudonym:<32} → {shown}")
    if not reveal:
        click.echo("")
        click.echo("  (values masked; pass --reveal to show them)")


def _mask(value: str) -> str:
    """Show only enough of a value to recognise it."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _ts(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
