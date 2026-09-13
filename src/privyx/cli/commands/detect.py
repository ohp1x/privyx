"""`privyx detect` command — detect sensitive entities in text."""

from __future__ import annotations

import asyncio
import sys

import click


@click.command()
@click.argument("text", nargs=-1, required=False)
@click.option("--stdin", "use_stdin", is_flag=True, help="Read text from stdin")
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.option("--policy/--no-policy", default=True, help="Apply the configured policy")
@click.option("--transform", is_flag=True, help="Also show the pseudonymized text")
def detect(
    text: tuple[str, ...],
    use_stdin: bool,
    config_path: str | None,
    policy: bool,
    transform: bool,
) -> None:
    """Detect sensitive entities in text.

    Uses the *configured* detector and policy, so what you see here is what the
    proxy would do with the same config.
    """
    from privyx.core.errors import PrivyxError

    if use_stdin:
        text = (sys.stdin.read(),)
    if not text:
        click.echo("Usage: privyx detect [--stdin] <text>")
        sys.exit(1)

    try:
        asyncio.run(_detect_async(" ".join(text), config_path, policy, transform))
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


async def _detect_async(
    text: str, config_path: str | None, apply_policy: bool, transform: bool
) -> None:
    from privyx.config.loader import load_config
    from privyx.core.builder import (
        build_anchor,
        build_detector_from,
        build_operator,
        build_policy_from,
    )
    from privyx.core.context import Context
    from privyx.core.session import Session

    settings = load_config(config_path)
    context = Context()

    detection = await build_detector_from(settings).detect(text, context)
    if apply_policy:
        detection = await build_policy_from(settings).decide(detection, context)

    if not detection.spans:
        click.echo("No sensitive entities detected.")
        return

    for span in detection.merged(text).spans:
        click.echo(f"  {span.start:>4}:{span.end:<4}  {span.entity_type:<16}  {span.text!r}")

    if transform:
        operator = build_operator(settings.operator.model_dump(), anchor=build_anchor(settings))
        result = await operator.pseudonymize(text, detection, Session(), context)
        click.echo("")
        click.echo(result.text)
