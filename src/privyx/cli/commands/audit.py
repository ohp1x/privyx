"""`privyx audit` command group — summarize and follow the audit trail.

Reads the JSONL file at ``audit.path`` (or a FILE argument), so the common
questions need no ``jq``.  The trail holds counts and entity types only, and
these commands print nothing more than it does.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, TextIO

import click

from privyx.cli.commands.session import _CONFIG, _duration, _ts
from privyx.observability.metrics import AuditStats

_FILE = click.argument("file", required=False, type=click.Path(dir_okay=False, path_type=Path))
_ENVELOPE = frozenset({"schema_version", "time", "ts", "event", "request_id", "session_id"})


@click.group("audit")
def audit() -> None:
    """Summarize and follow the audit trail (FILE defaults to audit.path)."""


@audit.command("stats")
@_FILE
@_CONFIG
@click.option(
    "--since", callback=_duration, help="Only count events from the last 30m, 12h, 7d, 2w."
)
def stats(file: Path | None, config_path: str | None, since: int | None) -> None:
    """Summarize requests, errors, sessions, and masked entity types."""
    cutoff = time.time() - since if since else None
    totals = AuditStats()
    skipped = 0
    path, fh = _open_log(file, config_path)
    with fh:
        for line in fh:
            try:
                record = json.loads(line)
                if cutoff is None or record["ts"] >= cutoff:
                    totals.add(record)
            except (ValueError, KeyError, TypeError):
                skipped += 1

    events = totals.events
    if not events:
        click.echo(f"No audit events in {path}")
    else:
        responses = events["proxy.response"]
        avg = f" (avg {totals.response_ms / responses / 1000:.2f}s)" if responses else ""
        click.echo(f"Audit log:  {path}")
        if totals.first_ts is not None and totals.last_ts is not None:
            click.echo(f"Period:     {_ts(totals.first_ts)} → {_ts(totals.last_ts)}")
        click.echo(f"Events:     {events.total()}")
        click.echo(f"Requests:   {events['proxy.request']}")
        click.echo(f"Responses:  {responses}{avg}")
        click.echo(f"Errors:     {events['proxy.error']}{_breakdown(totals.errors)}")
        click.echo(
            f"Sessions:   {events['session.created']} created, {events['session.deleted']} deleted"
        )
        click.echo(f"Masked:     {totals.entities.total()} entities")
        click.echo(f"Restored:   {totals.restored} pseudonyms")
        if totals.detector:
            counts = ", ".join(f"{k} {v}" for k, v in sorted(totals.detector.items()))
            click.echo(f"Detector:   {counts}")
        if totals.entities:
            width = max(len(name) for name in totals.entities)
            click.echo("\nMasked by entity type:")
            for name, count in totals.entities.most_common():
                click.echo(f"  {name:<{width}}  {count}")
    if skipped:
        click.echo(f"Skipped {skipped} unreadable line(s)", err=True)


@audit.command("tail")
@_FILE
@_CONFIG
@click.option("-n", "--lines", default=10, show_default=True, help="Recent events to show first.")
@click.option(
    "--follow/--no-follow",
    default=True,
    show_default=True,
    help="Keep printing events as they are written (Ctrl-C to stop).",
)
def tail(file: Path | None, config_path: str | None, lines: int, follow: bool) -> None:
    """Print recent audit events one readable line each, then follow new ones."""
    path, fh = _open_log(file, config_path)
    with fh:
        # ponytail: reads the whole file to reach the last N lines; seek from
        # the end instead if logs grow to gigabytes.
        for line in deque(fh, maxlen=max(lines, 0)):
            click.echo(_pretty(line))
        if not follow:
            return
        partial = ""
        try:
            while True:
                chunk = fh.readline()
                if not chunk:
                    try:
                        if path.stat().st_size < fh.tell():  # truncated by copytruncate
                            fh.seek(0)
                    except OSError:
                        pass  # moved away mid-rotation; keep reading the open file
                    time.sleep(0.5)
                    continue
                partial += chunk
                if partial.endswith("\n"):  # never print a line caught mid-write
                    click.echo(_pretty(partial))
                    partial = ""
        except KeyboardInterrupt:
            pass


def _open_log(file: Path | None, config_path: str | None) -> tuple[Path, TextIO]:
    from privyx.config.loader import load_config
    from privyx.core.errors import PrivyxError

    try:
        path = file or Path(os.path.expanduser(load_config(config_path).audit.path))
        return path, path.open(encoding="utf-8", errors="replace")
    except (PrivyxError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


def _pretty(line: str) -> str:
    """One audit record as ``time  event  request  session  key=value …``."""
    try:
        record = json.loads(line)
        event = record["event"]
        extras = " ".join(f"{k}={_value(v)}" for k, v in record.items() if k not in _ENVELOPE)
        text = (
            f"{_ts(record['ts'])}  {event:<17}  {record.get('request_id') or '-':<16}  "
            f"{record.get('session_id') or '-'}  {extras}"
        )
    except Exception:  # not a record we can read: show it as written
        return line.rstrip("\n")
    return click.style(text, fg="red") if event == "proxy.error" else text


def _value(value: Any) -> str:
    if isinstance(value, dict):  # entity_counts
        return ",".join(f"{k}:{v}" for k, v in value.items())
    return value if isinstance(value, str) else json.dumps(value)


def _breakdown(counts: Counter[str]) -> str:
    return f" ({', '.join(f'{k} {v}' for k, v in counts.most_common())})" if counts else ""
