"""`privyx detect` command — detect sensitive entities in text."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

import click

from privyx.cli.config import config_options

if TYPE_CHECKING:
    from privyx.app import Privyx
    from privyx.config.schema import Settings
    from privyx.core import Context, Span


@click.command()
@click.argument("text", nargs=-1, required=False)
@click.option("--stdin", "use_stdin", is_flag=True, help="Read text from stdin")
@config_options
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
    from privyx.core import PrivyxError

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
    from privyx.app import load_config
    from privyx.app._build import build_detector_from, build_policy_from, open_operator
    from privyx.app._plugins import load_plugins
    from privyx.core import Context, Session

    settings = load_config(config_path)
    # Register plugin component types so a configured plugin detector/operator
    # resolves.  No app.starting or app.stopping: this is a one-shot query, not a server.
    app = load_plugins(settings)
    context = Context()
    if explain:
        await _explain(text, settings, apply_policy, context, app)
        return

    detection = await build_detector_from(settings, app).detect(text, context)
    if apply_policy:
        detection = await build_policy_from(settings, app).decide(detection, context)

    if not detection.spans:
        click.echo("No sensitive entities detected.")
        return

    for span in detection.merged(text).spans:
        click.echo(f"  {span.start:>4}:{span.end:<4}  {span.entity_type:<16}  {span.text!r}")

    if transform:
        operator = await open_operator(settings, app)
        result = await operator.pseudonymize(text, detection, Session(), context)
        click.echo("")
        click.echo(result.text)


async def _explain(
    text: str, settings: Settings, apply_policy: bool, context: Context, app: Privyx
) -> None:
    """Print each span with the detectors that found it and the policy's decision.

    Each configured detector runs on its own, so a span can be traced to the
    one that found it; together they find what the configured detector does.
    """
    from privyx.app._build import build_detector, build_policy_from
    from privyx.config.schema import DetectorConfig
    from privyx.core import BUILTIN_ENTITIES, Detection

    async def detect(config: dict[str, Any]) -> Detection:
        return await build_detector(DetectorConfig.model_validate(config), app).detect(
            text, context
        )

    sections = settings.detector if isinstance(settings.detector, list) else [settings.detector]
    found: list[tuple[Span, str]] = []
    for i, section in enumerate(sections, 1):
        label = section.type if len(sections) == 1 else f"#{i} {section.type}"
        config = section.model_dump()
        options = _PatternOptions(config)
        detection = await detect(config)
        plain = decoded = detection
        if options.decode or options.normalize:
            plain = await detect({**config, "decode": False, "normalize": False})
        if options.decode and options.normalize:
            decoded = await detect({**config, "normalize": False})
        direct = {(s.start, s.end, s.entity_type) for s in plain.spans}
        hidden = (
            {(s.start, s.end, s.entity_type) for s in decoded.spans} if options.decode else set()
        )
        # Merged with terms, a pattern's entity is credited to the terms they find alone.
        both = options.terms.keys() & options.patterns.keys() if options.merge_terms else set()
        by_terms: set[tuple[int, int, str]] = set()
        if both and section.type in ("regex", "yaml"):
            # The terms alone: no pattern of theirs, nor the built-in it replaced.
            patterns = {k: v for k, v in options.patterns.items() if k not in both}
            builtin = both & BUILTIN_ENTITIES
            alone = await detect(
                {
                    **config,
                    "patterns": patterns,
                    "enable": [e for e in options.enable if e not in both],
                    "disable": sorted({*options.disable, *builtin}),
                }
            )
            by_terms = {(s.start, s.end, s.entity_type) for s in alone.spans}
        for s in detection.spans:
            where = (s.start, s.end, s.entity_type)
            merged = where in by_terms and s.entity_type in both
            reason = f"{label}: {_origin(section.type, options, s.entity_type, merged)}"
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
        kept = await build_policy_from(settings, app).decide(raw, context)
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


class _PatternOptions:
    """The pattern options of a detector section, as their defaults for a type without them."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.decode = bool(config.get("decode"))
        self.normalize = bool(config.get("normalize"))
        self.terms: dict[str, Any] = config.get("terms") or {}
        self.patterns: dict[str, Any] = config.get("patterns") or {}
        self.merge_terms = bool(config.get("merge_terms", True))
        self.enable: list[str] = config.get("enable") or []
        self.disable: list[str] = config.get("disable") or []


def _origin(kind: str, options: _PatternOptions, entity: str, by_terms: bool) -> str:
    """Which rule of a detector found a span: a pattern, a term list, or a built-in.

    ``by_terms``: the terms find the span alone, which decides for an entity
    whose terms and pattern are merged.
    """
    from privyx.detectors._pattern import DEFAULT_PATTERNS

    if kind not in ("regex", "yaml"):
        return entity
    if entity in options.patterns and not by_terms:
        return f"pattern {entity}"
    if entity in options.terms:
        return f"terms {entity}"
    if (kind == "regex" and entity in DEFAULT_PATTERNS) or entity in options.enable:
        return f"built-in pattern {entity}"
    return entity


def _policy_reason(settings: Settings) -> str:
    policy = settings.policy
    if policy.type == "strict":
        allowed = policy.model_dump().get("allowed")
        listed = "the types in policy.allowed" if allowed else "the built-in types"
        return f"the strict policy masks only {listed}"
    return f"the {policy.type} policy left it out"


def _overlap(a: Span, b: Span) -> bool:
    return a.start < b.end and b.start < a.end
