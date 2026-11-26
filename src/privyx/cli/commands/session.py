"""`privyx session` command group — list, show, prune, and export vault sessions."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from privyx.cli._options import config_options, json_option

if TYPE_CHECKING:
    from privyx.app._services._sessions import Sessions, Usage

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _duration(_ctx: click.Context, _param: click.Parameter, value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+)([smhdw])", value.strip())
    if match is None:
        raise click.BadParameter("use a number and a unit (s, m, h, d, w), e.g. 7d")
    return int(match[1]) * _UNITS[match[2]]


@click.group("session")
def session() -> None:
    """List, show, and prune vault sessions."""


@session.command("list")
@config_options
@json_option
def list_cmd(as_json: bool) -> None:
    """List live sessions, most recently active first.

    Shows ids, timestamps, and mapping counts only — never mapping values.
    """
    _run(_list(as_json))


@session.command("show")
@click.argument("session_id")
@config_options
@click.option("--reveal", is_flag=True, help="Show original values in full (sensitive!)")
def show(session_id: str, reveal: bool) -> None:
    """Show one session's mappings.

    Original values are masked unless --reveal is given, so that routine
    debugging does not print PII to a terminal or CI log.
    """
    _run(_show(session_id, reveal))


@session.command("prune")
@click.option(
    "--older-than",
    required=True,
    callback=_duration,
    help="Delete sessions idle at least this long: 30m, 12h, 7d, 2w.",
)
@config_options
@click.option("--dry-run", is_flag=True, help="List what would be deleted; delete nothing.")
def prune(older_than: int, dry_run: bool) -> None:
    """Delete sessions idle longer than --older-than.

    Each deletion is written to the audit trail as a session.deleted event
    with the reason prune.
    """
    _run(_prune(older_than, dry_run))


@session.command("export")
@click.argument("session_id")
@config_options
@click.option(
    "--forensic",
    is_flag=True,
    required=True,
    help="What went over the network and its events, without original values.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Archive to write [default: SESSION_ID-forensic.zip].",
)
def export(session_id: str, forensic: bool, output: Path | None) -> None:
    """Write a session's record for review as a zip archive.

    events.jsonl holds its audit events, transcript.jsonl each request as
    sent upstream and each reply as the upstream sent it (from the tap, when
    proxy.tap was on), and summary.json the totals.  Original values are left
    out: the transcript is the masked side.  A value Privyx did not detect is
    in it as it was sent.
    """
    _run(_export(session_id, output or Path(f"{session_id}-forensic.zip")))


@session.command("token")
@click.argument("token")
@click.option("--reveal", is_flag=True, help="Show the original value in full (sensitive!)")
@config_options
def token(token: str, reveal: bool) -> None:
    """Show what TOKEN stands for, and in which session."""
    _run(_token(token, reveal))


def _run(coro: Coroutine[Any, Any, None]) -> None:
    from privyx.core import PrivyxError

    try:
        asyncio.run(coro)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _sessions() -> Sessions:
    from privyx.app import Privyx

    return Privyx().sessions


async def _list(as_json: bool) -> None:
    service = _sessions()
    sessions = await service.list()
    if as_json:
        listed = [
            {
                "session_id": s.session_id,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "mappings": len(s.mapping),
            }
            for s in sessions
        ]
        click.echo(json.dumps({"vault": service.vault_type, "sessions": listed}, indent=2))
        return
    if not sessions:
        click.echo(f"No sessions (vault: {service.vault_type})")
        return
    width = max(len("SESSION"), *(len(s.session_id) for s in sessions))
    click.echo(f"{'SESSION':<{width}}  {'LAST ACTIVE':<19}  {'CREATED':<19}  MAPPINGS")
    for s in sessions:
        click.echo(
            f"{s.session_id:<{width}}  {_ts(s.updated_at)}  {_ts(s.created_at)}  {len(s.mapping)}"
        )
    click.echo(f"\n{len(sessions)} session(s) (vault: {service.vault_type})")


async def _show(session_id: str, reveal: bool) -> None:
    service = _sessions()
    view = await service.get(session_id)
    record, usage = view.record, view.usage
    if record is None and usage is None:
        click.echo(f"Session not found: {session_id}", err=True)
        click.echo(f"(vault: {service.vault_type})", err=True)
        sys.exit(1)

    click.echo(f"Session: {session_id}")
    click.echo(f"  Vault:      {service.vault_type}")
    if record is None:
        click.echo("  (not in the vault; usage from the audit trail)")
    else:
        click.echo(f"  Created:    {_ts(record.created_at)}")
        click.echo(f"  Updated:    {_ts(record.updated_at)}")
        click.echo(f"  Mappings:   {len(record.mapping)}")
        if record.metadata:
            click.echo(f"  Metadata:   {record.metadata}")
    if usage is not None:
        _show_usage(usage)
    if record is None or not record.mapping:
        return
    click.echo("")
    for pseudonym, original in record.mapping.items():
        shown = original if reveal else _mask(original)
        click.echo(f"  {pseudonym:<32} → {shown}")
    if not reveal:
        click.echo("")
        click.echo("  (values masked; pass --reveal to show them)")


async def _export(session_id: str, output: Path) -> None:
    exported = await _sessions().export(session_id, output)
    if exported is None:
        click.echo(f"Nothing recorded for session {session_id}", err=True)
        sys.exit(1)
    click.echo(
        f"Wrote {output}: {exported.events} event(s), {exported.transcript} transcript record(s)"
        + ("" if exported.transcript else " (proxy.tap was off)")
    )


def _show_usage(usage: Usage) -> None:
    from collections import Counter

    from privyx.cli.commands.audit import _tokens

    click.echo(f"  Responses:  {usage.responses}")
    if not usage.by_model:
        return
    total: Counter[str] = Counter()
    for tokens in usage.by_model.values():
        total.update(tokens)
    click.echo(f"  Tokens:     {_tokens(total)}")
    known = [c for c in usage.costs.values() if c is not None]
    unknown = sorted(model for model, c in usage.costs.items() if c is None)
    if known:
        models = ", ".join(sorted(m for m, c in usage.costs.items() if c is not None))
        click.echo(f"  Cost:       ~${sum(known):.4f} (estimate; {models})")
    if unknown:
        click.echo(f"  No price for: {', '.join(unknown)} (set features.audit.prices)")


async def _prune(older_than: int, dry_run: bool) -> None:
    service = _sessions()
    pruned = await service.prune(older_than, dry_run=dry_run)
    for s in pruned.sessions:
        click.echo(f"  {s.session_id}  last active {_ts(s.updated_at)}")
    verb = "Would prune" if dry_run else "Pruned"
    click.echo(
        f"{verb} {len(pruned.sessions)} session(s) idle since before {_ts(pruned.cutoff)} "
        f"(vault: {service.vault_type})"
    )


async def _token(token: str, reveal: bool) -> None:
    service = _sessions()
    found = await service.find_token(token)
    if found.parsed is None:
        click.echo(f"Not a token in the configured format ({found.format})")
    else:
        parsed = found.parsed
        click.echo(f"Type: {parsed.type}  Namespace: {parsed.namespace}  Id: {parsed.identifier}")
    if not found.sessions:
        click.echo(f"Not found in any session (vault: {service.vault_type})", err=True)
        sys.exit(1)
    for s in found.sessions:
        original = s.mapping[token]
        click.echo("")
        click.echo(f"Session: {s.session_id}")
        click.echo(f"  Value:       {original if reveal else _mask(original)}")
        click.echo(f"  Created:     {_ts(s.created_at)}")
        click.echo(f"  Last active: {_ts(s.updated_at)}")
        expires = _ts(s.updated_at + found.ttl) if found.ttl else "never (no vault.ttl)"
        click.echo(f"  Expires:     {expires}")
    if not reveal:
        click.echo("")
        click.echo("(value masked; pass --reveal to show it)")


def _mask(value: str) -> str:
    """Show only enough of a value to recognise it."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _ts(epoch: float) -> str:
    from privyx.events import local_time

    return local_time(epoch)
