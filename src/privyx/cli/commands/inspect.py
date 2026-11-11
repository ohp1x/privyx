"""`privyx inspect` command group — inspect internal state."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from privyx.config.schema import Settings

_CONFIG = click.option("--config", "-c", "config_path", default=None, help="Config file path")

#: Text the regex benchmark scans: prose, code, JSON, and a log line, with no
#: personal data, so the timing reflects the text a pattern mostly sees.
_SAMPLE = (
    "The quarterly report is due on Friday; please review section 3 before the call.\n"
    "def handler(event, context):\n    return {'status': 200, 'body': event['body']}\n"
    '{"id": 4821, "items": [{"sku": "A-100", "qty": 2}], "note": "ship by noon"}\n'
    "2026-01-15T09:30:00Z INFO worker-7 processed 128 jobs in 3.2s (retries=0)\n"
)


#: Seconds the regex benchmark may run before the pattern is reported as stalled.
_BENCH_TIMEOUT = 5.0

_BENCH = """
import re, sys, time
pattern = re.compile(sys.argv[1])
text = sys.stdin.read()
best = float("inf")
for _ in range(5):
    start = time.perf_counter()
    for _ in pattern.finditer(text):
        pass
    best = min(best, time.perf_counter() - start)
print(best)
"""


@click.group("inspect")
def inspect() -> None:
    """Inspect Privyx internal state."""


@inspect.command("routes")
@_CONFIG
def routes(config_path: str | None) -> None:
    """Show which request paths are masked, and with which wire schema."""
    from privyx.config.redact import redact
    from privyx.gateway.server import served_routes
    from privyx.providers.registry import resolve_base_url

    settings = _load(config_path)
    proxy = settings.proxy
    table = proxy.routes
    if proxy.mode == "gateway":
        table = served_routes(table, google=settings.provider.type == "google")
    click.echo(f"Mode: {proxy.mode}  Upstream: {redact(resolve_base_url(settings), 'url')}")
    click.echo("")
    width = max(len("PATH"), *(len(path) for path in table)) if table else len("PATH")
    click.echo(f"{'PATH':<{width}}  SCHEMA")
    for path, schema in table.items():
        click.echo(f"{path:<{width}}  {schema}")
    click.echo("")
    if proxy.mode == "gateway":
        click.echo("Other paths: 404")
    elif proxy.passthrough_unknown:
        click.echo("Other paths: forwarded unchanged, without masking")
    else:
        click.echo("Other paths: refused with 403 (proxy.passthrough_unknown is off)")


@inspect.command("regex")
@click.argument("pattern")
@_CONFIG
def regex(pattern: str, config_path: str | None) -> None:
    """Check a regex: an entity name from the config, or a pattern.

    Reports whether it can backtrack catastrophically and how fast it scans
    ordinary text.
    """
    from privyx.privacy.detector.yaml import backtracks

    entity = None
    configured = _configured_patterns(config_path)
    if pattern in configured:
        entity, pattern = pattern, configured[pattern]
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        click.echo(f"Error: invalid regex: {exc}", err=True)
        sys.exit(1)

    if entity:
        click.echo(f"Entity:  {entity}")
    click.echo(f"Pattern: {pattern}")
    if backtracks(pattern):
        click.echo("ReDoS:   risky: a repeat in a repeat, like (a+)+, can stall on crafted input")
    else:
        click.echo("ReDoS:   no nested repeat found")
    text = _SAMPLE * (64 * 1024 // len(_SAMPLE))
    best = _bench(compiled.pattern, text)
    if best is None:
        click.echo(
            f"Speed:   stalled: still scanning 64 KB of sample text after {_BENCH_TIMEOUT:g} s"
        )
    else:
        click.echo(f"Speed:   {best / (len(text) / 1024) * 1e6:.2f} µs per KB of sample text")


@inspect.command("token")
@click.argument("token")
@click.option("--reveal", is_flag=True, help="Show the original value in full (sensitive!)")
@_CONFIG
def token(token: str, reveal: bool, config_path: str | None) -> None:
    """Show what a token stands for, and in which session."""
    from privyx.cli.commands.session import _run

    _run(_token(token, reveal, config_path))


async def _token(token: str, reveal: bool, config_path: str | None) -> None:
    from privyx.cli.commands.session import _mask, _open_vault, _ts
    from privyx.core.builder import build_codec

    async with _open_vault(config_path) as (settings, vault):
        sessions = [s for s in await vault.list_sessions() if token in s.mapping]

    parsed = build_codec(settings).parse(token)
    if parsed is None:
        click.echo(f"Not a token in the configured format ({settings.token.format})")
    else:
        click.echo(f"Type: {parsed.type}  Namespace: {parsed.namespace}  Id: {parsed.identifier}")
    if not sessions:
        click.echo(f"Not found in any session (vault: {settings.vault.type})", err=True)
        sys.exit(1)
    ttl = settings.vault.ttl
    for s in sessions:
        original = s.mapping[token]
        click.echo("")
        click.echo(f"Session: {s.session_id}")
        click.echo(f"  Value:       {original if reveal else _mask(original)}")
        click.echo(f"  Created:     {_ts(s.created_at)}")
        click.echo(f"  Last active: {_ts(s.updated_at)}")
        click.echo(f"  Expires:     {_ts(s.updated_at + ttl) if ttl else 'never (no vault.ttl)'}")
    if not reveal:
        click.echo("")
        click.echo("(value masked; pass --reveal to show it)")


def _load(config_path: str | None) -> Settings:
    from privyx.config.loader import load_config
    from privyx.core.errors import PrivyxError

    try:
        return load_config(config_path)
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _configured_patterns(config_path: str | None) -> dict[str, str]:
    """Entity → regex of every regex/yaml detector configured, built-ins included."""
    from privyx.core.errors import PrivyxError
    from privyx.privacy.detector.builtin import DEFAULT_PATTERNS
    from privyx.privacy.detector.yaml import _enabled, _patterns_from

    settings = _load(config_path)
    sections = settings.detector if isinstance(settings.detector, list) else [settings.detector]
    patterns: dict[str, str] = {}
    for section in sections:
        if section.type not in ("regex", "yaml"):
            continue
        try:
            own = {**_enabled(section.model_dump()), **_patterns_from(section.model_dump())}
        except PrivyxError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        patterns.update({**DEFAULT_PATTERNS, **own} if section.type == "regex" else own)
    return patterns


def _bench(pattern: str, text: str) -> float | None:
    """Seconds for the best of five scans of ``text``; ``None`` past :data:`_BENCH_TIMEOUT`.

    Runs in a child process: a regex cannot be stopped from inside this one,
    and the static check does not catch every pattern that backtracks.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _BENCH, pattern],
            input=text,
            capture_output=True,
            text=True,
            timeout=_BENCH_TIMEOUT,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return None
    return float(done.stdout)
