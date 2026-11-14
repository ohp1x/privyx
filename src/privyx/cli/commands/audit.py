"""`privyx audit` command group — summarize and follow the audit trail.

Reads the JSONL file at ``audit.path`` (or a FILE argument), so the common
questions need no ``jq``.  The trail holds counts and entity types only, and
these commands print nothing more than it does.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from collections import Counter, deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

import click

from privyx.cli.commands.session import _duration, _ts
from privyx.cli.config import config_options
from privyx.observability.metrics import AuditStats

_FILE = click.argument("file", required=False, type=click.Path(dir_okay=False, path_type=Path))
_ENVELOPE = frozenset({"schema_version", "time", "ts", "event", "request_id", "session_id"})


@click.group("audit")
def audit() -> None:
    """Summarize and follow the audit trail (FILE defaults to audit.path)."""


@audit.command("stats")
@_FILE
@config_options
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
        click.echo(f"Passthrough: {events['proxy.passthrough']} (body unmasked)")
        click.echo(f"Blocked:    {events['proxy.blocked']} (unknown route)")
        click.echo(
            f"Sessions:   {events['session.created']} created, {events['session.deleted']} deleted"
        )
        click.echo(f"Masked:     {totals.entities.total()} entities")
        click.echo(f"Restored:   {totals.restored} pseudonyms")
        if totals.tokens:
            click.echo(f"Tokens:     {_tokens(totals.tokens)}")
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
@config_options
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


@audit.command("watch")
@click.argument("session_id", required=False)
@config_options
@click.option("-n", "--lines", default=10, show_default=True, help="Recent records to show first.")
@click.option(
    "--follow/--no-follow",
    default=True,
    show_default=True,
    help="Keep showing records as they are written (Ctrl-C to stop).",
)
def watch(session_id: str | None, config_path: str | None, lines: int, follow: bool) -> None:
    """Show what masking changed in each exchange, from the tap (audit.tap).

    One SESSION_ID, or every session.  Removed text is shown as [-...-], added
    text as {+...+}.  The tap holds original values, and so does this output.
    """
    from privyx.config.loader import load_config
    from privyx.core.errors import PrivyxError
    from privyx.observability.tap import tap_file

    try:
        settings = load_config(config_path)
    except PrivyxError as exc:
        raise click.ClickException(str(exc)) from exc
    directory = Path(os.path.expanduser(settings.audit.tap_dir))
    if not settings.audit.tap:
        click.echo("audit.tap is off: nothing new is recorded (set audit.tap: true)", err=True)

    def files() -> list[Path]:
        if session_id is not None:
            return [tap_file(directory, session_id)]
        return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []

    def new_records() -> list[dict[str, Any]]:
        records = _read_new(files(), offsets)
        # A file can be shared (a case-insensitive file system): check whose.
        return [r for r in records if session_id is None or r.get("session_id") == session_id]

    offsets: dict[Path, int] = {}
    pending = new_records()
    for record in pending[-lines:] if lines > 0 else []:
        click.echo(_render(record))
    if not follow:
        return
    try:
        while True:
            time.sleep(0.5)
            for record in new_records():
                click.echo(_render(record))
    except KeyboardInterrupt:
        pass


def _read_new(paths: list[Path], offsets: dict[Path, int]) -> list[dict[str, Any]]:
    """The records written to ``paths`` since ``offsets``, oldest first."""
    records = []
    for path in paths:
        try:
            with path.open("rb") as fh:
                if path.stat().st_size < offsets.get(path, 0):
                    offsets[path] = 0  # truncated or replaced
                fh.seek(offsets.get(path, 0))
                data = fh.read()
        except OSError:
            continue
        whole = data[: data.rfind(b"\n") + 1]  # never a line caught mid-write
        offsets[path] = offsets.get(path, 0) + len(whole)
        for line in whole.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return sorted(records, key=lambda r: r.get("ts") or 0)


def _render(record: dict[str, Any]) -> str:
    stage = str(record.get("stage"))
    if stage == "request":
        where = record.get("path") or ""
        changes = _changes(record.get("before"), record.get("after"))
    else:
        where = f"status {record.get('status')}" + (" stream" if record.get("stream") else "")
        changes = _changes(record.get("upstream"), record.get("restored"))
    head = (
        f"{_ts(record['ts']) if isinstance(record.get('ts'), int | float) else '-'}  "
        f"{stage:<8}  {record.get('request_id') or '-'}  {record.get('session_id') or '-'}  {where}"
    )
    body = [f"  {line}" for line in changes] or ["  (no change)"]
    return "\n".join([click.style(head, bold=True), *body])


#: Keys whose string values are the text of a streamed event.
_DELTA_KEYS = frozenset({"text", "partial_json", "thinking", "content", "delta", "arguments"})
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
_CONTEXT = 30
_MAX = 200


def _changes(before: Any, after: Any) -> list[str]:
    """One line per place where ``after`` differs from ``before``."""
    if isinstance(before, str) and isinstance(after, str):
        return _word_diff(_sse_text(before), _sse_text(after))
    old, new = list(_leaves(before)), list(_leaves(after))
    lines: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if op == "equal":
            continue
        pairs = min(i2 - i1, j2 - j1) if op == "replace" else 0
        for a, b in zip(old[i1 : i1 + pairs], new[j1 : j1 + pairs], strict=True):
            lines += _word_diff(a, b)
        lines += [_removed(text) for text in old[i1 + pairs : i2]]
        lines += [_added(text) for text in new[j1 + pairs : j2]]
    return lines


def _word_diff(before: str, after: str) -> list[str]:
    """Each changed run of ``after`` with some context, as ``[-old-]{+new+}``."""
    old, new = _TOKEN.findall(before), _TOKEN.findall(after)
    segments: list[list[Any]] = []  # [changed, old text, new text]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        segments.append([op != "equal", "".join(old[i1:i2]), "".join(new[j1:j2])])
    # A sliver left equal inside a change (the dot of an address) joins it.
    for k in range(1, len(segments) - 1):
        _, text, _ = segments[k]
        if not segments[k][0] and len(text) <= 2 and not text.isspace():
            segments[k][0] = segments[k - 1][0] and segments[k + 1][0]
    merged: list[list[Any]] = []
    for segment in segments:
        if merged and segment[0] and merged[-1][0]:
            merged[-1][1] += segment[1]
            merged[-1][2] += segment[2]
        else:
            merged.append(segment)
    lines = []
    for k, (changed, removed, added) in enumerate(merged):
        if not changed:
            continue
        left = merged[k - 1][1] if k > 0 else ""
        right = merged[k + 1][1] if k + 1 < len(merged) else ""
        # What both sides still share after merging belongs to the context.
        head = len(os.path.commonprefix([removed, added]))
        tail = len(os.path.commonprefix([removed[head:][::-1], added[head:][::-1]]))
        left, right = left + removed[:head], removed[len(removed) - tail :] + right
        removed, added = removed[head : len(removed) - tail], added[head : len(added) - tail]
        left = ("…" + left[-_CONTEXT:]) if len(left) > _CONTEXT else left
        right = (right[:_CONTEXT] + "…") if len(right) > _CONTEXT else right
        parts = [_oneline(left)]
        parts += [_removed(removed)] if removed else []
        parts += [_added(added)] if added else []
        lines.append("".join([*parts, _oneline(right)]))
    return lines


def _removed(text: str) -> str:
    return click.style(f"[-{_short(text)}-]", fg="red")


def _added(text: str) -> str:
    return click.style(f"{{+{_short(text)}+}}", fg="green")


def _short(text: str) -> str:
    text = _oneline(text)
    return text if len(text) <= _MAX else text[:_MAX] + "…"


def _oneline(text: str) -> str:
    return text.replace("\n", "\\n")


def _leaves(value: Any) -> Any:
    """Every string value in ``value``, in order; keys are structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)


def _sse_text(text: str) -> str:
    """The text an SSE stream carries, its deltas joined; other text as it is."""
    if not text.startswith(("data:", "event:", ":")) and "\ndata:" not in text:
        return text
    parts: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in _DELTA_KEYS and isinstance(item, str):
                    parts.append(item)
                else:
                    collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                collect(json.loads(line[5:]))
            except ValueError:
                continue
    return "".join(parts)


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
        text = _line(record)
    except Exception:  # not a record we can read: show it as written
        return line.rstrip("\n")
    return click.style(text, fg="red") if record["event"] == "proxy.error" else text


def _line(record: dict[str, Any]) -> str:
    """``record`` as one plain line; raises on a record it cannot read."""
    extras = " ".join(f"{k}={_value(v)}" for k, v in record.items() if k not in _ENVELOPE)
    return (
        f"{_ts(record['ts'])}  {record['event']:<17}  {record.get('request_id') or '-':<16}  "
        f"{record.get('session_id') or '-'}  {extras}"
    )


def _value(value: Any) -> str:
    if isinstance(value, dict):  # entity_counts
        return ",".join(f"{k}:{v}" for k, v in value.items())
    return value if isinstance(value, str) else json.dumps(value)


def _tokens(usage: Mapping[str, int]) -> str:
    """Token counts in one line, with the prompt cache hit rate."""
    from privyx.observability.usage import cache_hit_rate

    text = (
        f"input {usage.get('input', 0):,}, output {usage.get('output', 0):,}, "
        f"cache read {usage.get('cache_read', 0):,}, cache write {usage.get('cache_write', 0):,}"
    )
    rate = cache_hit_rate(usage)
    return text if rate is None else f"{text} (cache hit rate {rate:.0%})"


def _breakdown(counts: Counter[str]) -> str:
    return f" ({', '.join(f'{k} {v}' for k, v in counts.most_common())})" if counts else ""
