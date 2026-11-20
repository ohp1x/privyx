"""`privyx lint` command — find spellings that slip past masking with a config."""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

from privyx.cli.config import config_options

if TYPE_CHECKING:
    from privyx.privacy.detector.base import Detector
    from privyx.privacy.policy.base import Policy

#: Detector options a missed spelling is retried with.  One is suggested only
#: when the retry masks the whole value, so a suggestion is never a guess.
_OPTIONS = ("underscore_separates_words", "decode", "normalize", "term_variants", "merge_terms")

#: Fake values of the built-in entities that appear inside identifiers
#: (``host_10.0.0.1``).  The others are found by what surrounds them.
_SAMPLES = {
    "IP_ADDRESS": "10.0.0.1",
    "SSN": "123-45-6789",
    "CREDIT_CARD": "4111111111111111",
    "API_KEY": "sk-" + "proj-abcdefghijklmnopqrstuvwx",
}

#: A term written as words split by spaces, dots, dashes, or underscores.
_WORDS = re.compile(r"[^\W_]+(?:[ ._-]+[^\W_]+)*")
_FULLWIDTH = {c: c + 0xFEE0 for c in range(0x21, 0x7F)}

#: A spelling as (text, sensitive) parts: the sensitive parts must be masked.
Parts = tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class _Probe:
    entity: str
    parts: Parts

    @property
    def text(self) -> str:
        return "".join(text for text, _ in self.parts)

    def sensitive(self) -> set[int]:
        positions: set[int] = set()
        at = 0
        for text, secret in self.parts:
            if secret:
                positions.update(range(at, at + len(text)))
            at += len(text)
        return positions


@click.command()
@config_options
def lint(config_path: str | None) -> None:
    """Show spellings of your terms and built-in values that are not masked.

    Each term in the config is tried in other spellings (DB_JOHN, JOHN__SMITH,
    john%20smith, zero-width and fullwidth characters), and a few fake built-in
    values inside identifiers (host_10.0.0.1), through the configured regex and
    yaml detectors and policy.  A detector option is suggested only when a
    retry with it masks the value.  Terms from another file that no detector
    uses are reported first.  Exits 1 when a spelling is not fully masked.
    """
    from privyx.core import PrivyxError

    try:
        leaks = asyncio.run(_lint(config_path))
    except PrivyxError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    sys.exit(1 if leaks else 0)


async def _lint(config_path: str | None) -> int:
    from privyx.app._plugins import load_plugins
    from privyx.cli.commands.detect import _policy_reason
    from privyx.config.loader import load_config
    from privyx.core.builder import build_policy_from
    from privyx.privacy.detector.yaml import build_detector

    unused: list[str] = []
    settings = load_config(config_path, warnings=unused)
    for warning in unused:
        click.echo(f"warning: {warning}")
    load_plugins(settings)
    sections = settings.detector if isinstance(settings.detector, list) else [settings.detector]
    checked = [s.model_dump() for s in sections if s.type in ("regex", "yaml")]
    for i, section in enumerate(sections, 1):
        if section.type not in ("regex", "yaml"):
            click.echo(f"Not checked: detector #{i} ({section.type})")
    if not checked:
        click.echo("No regex or yaml detector to check.")
        return 0

    policy = build_policy_from(settings)
    off = frozenset(o for o in _OPTIONS if not all(c.get(o) for c in checked))
    built: dict[frozenset[str], Detector] = {}
    prefix, suffix = ("detector.", "")
    if len(sections) > 1:
        prefix, suffix = "", " on each regex or yaml detector"

    def detector_with(options: frozenset[str]) -> Detector:
        if options not in built:
            on = dict.fromkeys(options, True)
            built[options] = build_detector([{**c, **on} for c in checked])
        return built[options]

    async def masks(probe: _Probe, options: frozenset[str], policy: Policy | None) -> bool:
        return not await _missing(probe, detector_with(options), policy)

    async def advice_for(probe: _Probe) -> str:
        """The options that mask ``probe``: each one alone, else the fewest together."""
        fixes = [o for o in sorted(off) if await masks(probe, frozenset({o}), policy)]
        joiner = " or "
        if not fixes and await masks(probe, off, policy):
            fix = set(off)
            for o in sorted(off):
                if await masks(probe, frozenset(fix - {o}), policy):
                    fix.discard(o)
            fixes, joiner = sorted(fix), " and "
        if fixes:
            return "Set " + joiner.join(f"{prefix}{o}: true" for o in fixes) + suffix
        if await masks(probe, off, None):
            return f"Found, but {_policy_reason(settings)}"
        return "No option masks these; add them to the terms"

    listed = {(e, str(v).strip()) for c in checked for e, vs in c["terms"].items() for v in vs}
    terms = sorted((entity, term) for entity, term in listed if term)
    probes: list[_Probe] = []
    for entity, term in terms:
        if len(term) <= 2:
            click.echo(f"warning: {entity} term {term!r} masks every standalone {term!r}")
        probes += [_Probe(entity, parts) for parts in _spellings(term)]
    samples = 0
    for entity, value in _SAMPLES.items():
        # A built-in the config turns off, or the policy drops, is not checked.
        if await masks(_Probe(entity, ((value, True),)), frozenset(), policy):
            samples += 1
            for parts in ((("id_", False), (value, True)), ((value, True), ("_old", False))):
                probes.append(_Probe(entity, parts))

    # Advice -> the spellings it masks, in the order first seen.
    groups: dict[str, list[str]] = {}
    for probe in probes:
        missing = await _missing(probe, detector_with(frozenset()), policy)
        if not missing:
            continue
        state = "partly" if missing != probe.sensitive() else "missed"
        advice = await advice_for(probe)
        groups.setdefault(advice, []).append(f"  {state:<7} {probe.entity:<14} {probe.text!r}")

    for advice, lines in groups.items():
        click.echo(advice)
        click.echo("\n".join(lines))
    leaks = sum(len(lines) for lines in groups.values())
    click.echo(
        f"Checked {_count(len(probes), 'spelling')} of {_count(len(terms), 'term')} "
        f"and {_count(samples, 'built-in value')}: "
        + (f"{leaks} not fully masked." if leaks else "all masked.")
    )
    return leaks


def _spellings(term: str) -> list[Parts]:
    """``term`` as written and in the spellings that tend to slip past a term list."""
    if not _WORDS.fullmatch(term):
        return [((term, True),)]
    pieces = re.split(r"([ ._-]+)", term)
    written: Parts = tuple((piece, i % 2 == 0) for i, piece in enumerate(pieces))
    upper: Parts = tuple((text.upper(), secret) for text, secret in written)
    words = pieces[::2]
    first = words[0]
    spellings = [
        written,
        (("DB_", False), *upper),
        (*written, ("_dev", False)),
        tuple((text.translate(_FULLWIDTH), secret) for text, secret in written),
    ]
    if len(first) > 1:
        spellings.append(((first[0], True), ("\u200b", False), (first[1:], True), *written[1:]))
    if len(words) > 1:
        for separator in ("_", "__", "%20"):
            joined = [((separator, False), (word, True)) for word in words[1:]]
            spellings.append(((first, True), *(part for pair in joined for part in pair)))
    return list(dict.fromkeys(spellings))


async def _missing(probe: _Probe, detector: Detector, policy: Policy | None) -> set[int]:
    """The sensitive positions of ``probe`` the detector and policy leave unmasked."""
    from privyx.core import Context

    text, context = probe.text, Context()
    detection = await detector.detect(text, context)
    if policy is not None:
        detection = await policy.decide(detection, context)
    masked = {i for s in detection.merged(text).spans for i in range(s.start, s.end)}
    return probe.sensitive() - masked


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' * (n != 1)}"
