"""`privyx detect` — detect sensitive entities in text, or check a regex."""

from __future__ import annotations

import asyncio
import sys

import click

from privyx.cli._options import config_options


@click.command()
@click.argument("text", nargs=-1, required=False)
@click.option("--stdin", "use_stdin", is_flag=True, help="Read text from stdin")
@config_options
@click.option("--policy/--no-policy", default=True, help="Apply the configured policy")
@click.option("--transform", is_flag=True, help="Also show the pseudonymized text")
@click.option(
    "--explain", is_flag=True, help="Show which detector found each span and whether it is masked"
)
@click.option(
    "--pattern", default=None, help="Check this regex: catastrophic backtracking and speed."
)
@click.option("--entity", default=None, help="Check the configured regex of this entity.")
def detect(
    text: tuple[str, ...],
    use_stdin: bool,
    policy: bool,
    transform: bool,
    explain: bool,
    pattern: str | None,
    entity: str | None,
) -> None:
    """Detect sensitive entities in text, or check a regex.

    Uses the *configured* detectors and policy, so what you see here is what
    the proxy would do with the same config.  --pattern or --entity checks a
    regex instead: whether it can backtrack catastrophically, and how fast it
    scans ordinary text.
    """
    from privyx.core import PrivyxError

    if pattern is not None or entity is not None:
        if text or use_stdin or (pattern is not None and entity is not None):
            raise click.UsageError("--pattern or --entity checks one regex: leave out the rest")
        try:
            check_regex(pattern, entity)
        except PrivyxError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        return
    if use_stdin:
        text = (sys.stdin.read(),)
    if not text:
        click.echo("Usage: privyx detect [--stdin] <text>")
        sys.exit(1)

    try:
        asyncio.run(_detect(" ".join(text), policy, transform, explain))
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


async def _detect(text: str, policy: bool, transform: bool, explain: bool) -> None:
    from privyx.app import Privyx

    inspection = Privyx().inspection
    if explain:
        explained = await inspection.explain(text, policy=policy)
        if not explained:
            click.echo("No sensitive entities detected.")
        for item in explained:
            span = item.span
            click.echo(f"  {span.start:>4}:{span.end:<4}  {span.entity:<16}  {span.text!r}")
            for reason in item.found_by:
                click.echo(f"        found by  {reason}")
            click.echo(f"        {item.outcome}")
        return
    spans = await inspection.spans(text, policy=policy)
    if not spans:
        click.echo("No sensitive entities detected.")
        return
    for span in spans:
        click.echo(f"  {span.start:>4}:{span.end:<4}  {span.entity:<16}  {span.text!r}")
    if transform:
        click.echo("")
        click.echo(await inspection.preview(text, policy=policy))


def check_regex(pattern: str | None, entity: str | None) -> None:
    """Print the check of ``pattern``, or of the configured regex of ``entity``."""
    from privyx.app import Privyx

    found = Privyx().inspection.check_pattern(pattern, entity=entity)
    if found.entity:
        click.echo(f"Entity:  {found.entity}")
    click.echo(f"Pattern: {found.pattern}")
    if found.risky:
        click.echo("ReDoS:   risky: a repeat in a repeat, like (a+)+, can stall on crafted input")
    else:
        click.echo("ReDoS:   no nested repeat found")
    if found.speed is None:
        click.echo(
            f"Speed:   stalled: still scanning 64 KB of sample text after {found.timeout:g} s"
        )
    else:
        click.echo(f"Speed:   {found.speed:.2f} µs per KB of sample text")
