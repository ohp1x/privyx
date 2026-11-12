"""`privyx detect` command — detect sensitive entities in text."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from privyx.config.schema import DetectorConfig, Settings
    from privyx.core.context import Context
    from privyx.core.result import Span


@click.command()
@click.argument("text", nargs=-1, required=False)
@click.option("--stdin", "use_stdin", is_flag=True, help="Read text from stdin")
@click.option("--config", "-c", "config_path", default=None, help="Config file path")
@click.option("--policy/--no-policy", default=True, help="Apply the configured policy")
@click.option("--transform", is_flag=True, help="Also show the pseudonymized text")
@click.option(
    "--explain", is_flag=True, help="Show which detector found each span and whether it is masked"
)
def detect(
    text: tuple[str, ...],
    use_stdin: bool,
    config_path: str | None,
    policy: bool,
    transform: bool,
    explain: bool,
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
        asyncio.run(_detect_async(" ".join(text), config_path, policy, transform, explain))
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


async def _detect_async(
    text: str, config_path: str | None, apply_policy: bool, transform: bool, explain: bool = False
) -> None:
    from privyx.config.loader import load_config
    from privyx.core.builder import (
        build_anchor,
        build_codec,
        build_detector_from,
        build_operator,
        build_policy_from,
    )
    from privyx.core.context import Context
    from privyx.core.session import Session
    from privyx.plugins.loader import load_plugins

    settings = load_config(config_path)
    # Register plugin component types so a configured plugin detector/operator
    # resolves.  No lifecycle hooks fire: this is a one-shot query, not a server.
    load_plugins(settings)
    context = Context()
    if explain:
        await _explain(text, settings, apply_policy, context)
        return

    detection = await build_detector_from(settings).detect(text, context)
    if apply_policy:
        detection = await build_policy_from(settings).decide(detection, context)

    if not detection.spans:
        click.echo("No sensitive entities detected.")
        return

    for span in detection.merged(text).spans:
        click.echo(f"  {span.start:>4}:{span.end:<4}  {span.entity_type:<16}  {span.text!r}")

    if transform:
        operator = build_operator(
            settings.operator.model_dump(),
            anchor=build_anchor(settings),
            codec=build_codec(settings),
        )
        result = await operator.pseudonymize(text, detection, Session(), context)
        click.echo("")
        click.echo(result.text)


async def _explain(text: str, settings: Settings, apply_policy: bool, context: Context) -> None:
    """Print each span with the detectors that found it and the policy's decision.

    Each configured detector runs on its own, so a span can be traced to the
    one that found it; together they find what the configured detector does.
    """
    from privyx.core.builder import build_policy_from
    from privyx.core.result import Detection
    from privyx.privacy.detector.yaml import build_detector

    sections = settings.detector if isinstance(settings.detector, list) else [settings.detector]
    found: list[tuple[Span, str]] = []
    for i, section in enumerate(sections, 1):
        label = section.type if len(sections) == 1 else f"#{i} {section.type}"
        config = section.model_dump()
        detection = await build_detector(config).detect(text, context)
        plain = decoded = detection
        if section.decode or section.normalize:
            plain = await build_detector({**config, "decode": False, "normalize": False}).detect(
                text, context
            )
        if section.decode and section.normalize:
            decoded = await build_detector({**config, "normalize": False}).detect(text, context)
        direct = {(s.start, s.end, s.entity_type) for s in plain.spans}
        hidden = (
            {(s.start, s.end, s.entity_type) for s in decoded.spans} if section.decode else set()
        )
        for s in detection.spans:
            reason = f"{label}: {_origin(section, s.entity_type)}"
            where = (s.start, s.end, s.entity_type)
            if where not in direct:
                reason += ", once joined or decoded" if where in hidden else ", once folded"
            found.append((s, reason))

    raw = Detection([span for span, _ in found])
    shown = raw.merged(text).spans
    if not shown:
        click.echo("No sensitive entities detected.")
        return
    kept = raw
    if apply_policy:
        kept = await build_policy_from(settings).decide(raw, context)
    masked = kept.merged(text).spans

    for span in shown:
        click.echo(f"  {span.start:>4}:{span.end:<4}  {span.entity_type:<16}  {span.text!r}")
        for part, reason in found:
            if _overlap(part, span):
                click.echo(f"        found by  {reason}")
        # The masked spans lie within the shown ones: the policy only drops spans.
        parts = [m for m in masked if _overlap(m, span)]
        if not parts:
            click.echo(f"        not masked: {_policy_reason(settings)}")
        elif sum(m.length for m in parts) == span.length:
            click.echo("        masked")
        else:
            only = ", ".join(f"{m.text!r} as {m.entity_type}" for m in parts)
            click.echo(f"        partly masked: only {only}; the rest is sent as is")


def _origin(section: DetectorConfig, entity: str) -> str:
    """Which rule of a detector names ``entity``: a pattern, a term list, or a built-in."""
    from privyx.privacy.detector.builtin import DEFAULT_PATTERNS

    if section.type not in ("regex", "yaml"):
        return entity
    if entity in section.patterns:
        return f"pattern {entity}"
    if entity in section.terms:
        return f"terms {entity}"
    if (section.type == "regex" and entity in DEFAULT_PATTERNS) or entity in section.enable:
        return f"built-in pattern {entity}"
    return entity


def _policy_reason(settings: Settings) -> str:
    policy = settings.policy
    if policy.type == "strict":
        listed = "the types in policy.allowed" if policy.allowed else "the built-in types"
        return f"the strict policy masks only {listed}"
    return f"the {policy.type} policy left it out"


def _overlap(a: Span, b: Span) -> bool:
    return a.start < b.end and b.start < a.end
