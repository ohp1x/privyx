"""`privyx session` command group — list, show, and prune vault sessions.

``privyx inspect session <id>`` predates this group and stays as an alias of
``privyx session show``.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import click

from privyx.cli.commands.inspect import inspect

if TYPE_CHECKING:
    from privyx.config.schema import Settings
    from privyx.vault.base import Vault

_CONFIG = click.option("--config", "-c", "config_path", default=None, help="Config file path")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _duration(_ctx: click.Context, _param: click.Parameter, value: str) -> int:
    match = re.fullmatch(r"(\d+)([smhdw])", value.strip())
    if match is None:
        raise click.BadParameter("use a number and a unit (s, m, h, d, w), e.g. 7d")
    return int(match[1]) * _UNITS[match[2]]


@click.group("session")
def session() -> None:
    """List, show, and prune vault sessions."""


@session.command("list")
@_CONFIG
def list_cmd(config_path: str | None) -> None:
    """List live sessions, most recently active first.

    Shows ids, timestamps, and mapping counts only — never mapping values.
    """
    _run(_list(config_path))


@session.command("show")
@click.argument("session_id")
@_CONFIG
@click.option("--reveal", is_flag=True, help="Show original values in full (sensitive!)")
def show(session_id: str, config_path: str | None, reveal: bool) -> None:
    """Show one session's mappings.

    Original values are masked unless ``--reveal`` is given, so that routine
    debugging does not print PII to a terminal or CI log.
    """
    _run(_show(session_id, config_path, reveal))


@session.command("prune")
@click.option(
    "--older-than",
    required=True,
    callback=_duration,
    help="Delete sessions idle at least this long: 30m, 12h, 7d, 2w.",
)
@_CONFIG
@click.option("--dry-run", is_flag=True, help="List what would be deleted; delete nothing.")
def prune(older_than: int, config_path: str | None, dry_run: bool) -> None:
    """Delete sessions idle longer than ``--older-than``.

    Each deletion is written to the audit trail as ``session.deleted`` with
    ``reason: prune``.
    """
    _run(_prune(older_than, config_path, dry_run))


inspect.add_command(show, name="session")


def _run(coro: Coroutine[Any, Any, None]) -> None:
    from privyx.core.errors import PrivyxError

    try:
        asyncio.run(coro)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@asynccontextmanager
async def _open_vault(config_path: str | None) -> AsyncIterator[tuple[Settings, Vault]]:
    from privyx.config.loader import load_config
    from privyx.core.builder import build_vault
    from privyx.plugins.loader import load_plugins

    settings = load_config(config_path)
    # Register plugin component types so a configured plugin vault resolves.
    load_plugins(settings)
    vault, close = await build_vault(settings)
    try:
        yield settings, vault
    finally:
        await close()


async def _list(config_path: str | None) -> None:
    async with _open_vault(config_path) as (settings, vault):
        sessions = await vault.list_sessions()

    if not sessions:
        click.echo(f"No sessions (vault: {settings.vault.type})")
        return
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    width = max(len("SESSION"), *(len(s.session_id) for s in sessions))
    click.echo(f"{'SESSION':<{width}}  {'LAST ACTIVE':<19}  {'CREATED':<19}  MAPPINGS")
    for s in sessions:
        click.echo(
            f"{s.session_id:<{width}}  {_ts(s.updated_at)}  {_ts(s.created_at)}  {len(s.mapping)}"
        )
    click.echo(f"\n{len(sessions)} session(s) (vault: {settings.vault.type})")


async def _show(session_id: str, config_path: str | None, reveal: bool) -> None:
    async with _open_vault(config_path) as (settings, vault):
        record = await vault.get(session_id)

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


async def _prune(older_than: int, config_path: str | None, dry_run: bool) -> None:
    from privyx.core.builder import build_audit_logger
    from privyx.observability.audit import AuditLogger

    cutoff = time.time() - older_than
    async with _open_vault(config_path) as (settings, vault):
        stale = [s for s in await vault.list_sessions() if s.updated_at < cutoff]
        audit = AuditLogger(None) if dry_run else build_audit_logger(settings)
        try:
            for s in stale:
                if not dry_run:
                    await vault.delete(s.session_id)
                    # After the delete succeeds, so the trail never claims a
                    # deletion that failed (same contract as the proxy).
                    audit.session_deleted(
                        s.session_id, reason="prune", mapping_count=len(s.mapping)
                    )
                click.echo(f"  {s.session_id}  last active {_ts(s.updated_at)}")
        finally:
            audit.close()

    verb = "Would prune" if dry_run else "Pruned"
    click.echo(
        f"{verb} {len(stale)} session(s) idle since before {_ts(cutoff)} "
        f"(vault: {settings.vault.type})"
    )


def _mask(value: str) -> str:
    """Show only enough of a value to recognise it."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _ts(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
