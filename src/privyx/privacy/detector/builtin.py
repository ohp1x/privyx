"""Built-in detectors shipped with Privyx.

These are intentionally dependency-free: pure regex + simple heuristics.
"""

from __future__ import annotations

import asyncio
import re

from privyx.core.context import Context
from privyx.core.result import Detection, Span
from privyx.privacy.detector.base import BaseDetector, Detector

#: The phone pattern accepts an optional ``+<country>`` prefix and 2-3 groups
#: separated by space/dot/dash.  The lookarounds keep it from biting a chunk out
#: of a longer digit run: ``(?<!\d[-.])`` and ``(?![-.]\d)`` stop it matching
#: inside an IP address or SSN, where a more specific pattern should win.
PHONE_PATTERN = (
    r"(?<![\w\d])(?<!\d[-.])"
    r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]\d{3,4}(?:[\s.-]\d{3,4})?"
    r"(?![\w\d])(?![-.]\d)"
)

#: Built-in entity patterns.  Mirrored by ``privyx.config.defaults.DEFAULTS``
#: so that YAML config and code-level defaults never drift apart.
DEFAULT_PATTERNS: dict[str, str] = {
    "EMAIL": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "PHONE": PHONE_PATTERN,
    "CREDIT_CARD": r"\b(?:\d[ -]*?){13,16}\b",
    "IP_ADDRESS": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
}


def _detect(compiled: list[tuple[str, re.Pattern[str]]], text: str) -> Detection:
    """Run each entity's regex over ``text``.

    A pattern with a ``value`` group marks only that group, so
    ``DB_PASSWORD=(?P<value>\\S+)`` hides the secret but leaves the variable
    name for the model to read.  A match whose ``value`` group did not take part
    (another alternative matched) marks the whole match; an empty span marks
    nothing.
    """
    detection = Detection()
    for entity, pattern in compiled:
        has_value = "value" in pattern.groupindex
        for match in pattern.finditer(text):
            group = "value" if has_value and match["value"] is not None else 0
            start, end = match.span(group)
            if start < end:
                detection.add(start, end, entity, match[group])
    return detection


class RegexDetector(BaseDetector):
    """Detect entities with a set of named regular expressions."""

    name = "regex"

    def __init__(self, patterns: dict[str, str] | None = None) -> None:
        super().__init__()
        patterns = patterns or dict(DEFAULT_PATTERNS)
        self._compiled = [(name, re.compile(pattern)) for name, pattern in patterns.items()]

    def detect_sync(self, text: str, context: Context) -> Detection:
        return _detect(self._compiled, text)


class YamlDetector(BaseDetector):
    """Composite detector configured from YAML (entity -> regex list).

    Example config:

    .. code-block:: yaml

        detectors:
          - name: custom
            type: regex
            patterns:
              LICENSE_PLATE: "[A-Z]{1,3}-\\d{1,4}"
    """

    name = "yaml"

    def __init__(self, rules: dict[str, str] | None = None) -> None:
        super().__init__()
        self._rules = rules or {}
        self._compiled = [(entity, re.compile(pattern)) for entity, pattern in self._rules.items()]

    def detect_sync(self, text: str, context: Context) -> Detection:
        return _detect(self._compiled, text)


class CompositeDetector:
    """Run several detectors concurrently and pool their spans.

    Overlaps are left in place on purpose: the policy must see every span before
    the operator merges them.  Merging first could fold an allowed EMAIL into a
    longer span of a type ``strict`` drops, and the email would leak.  Only exact
    duplicates (two detectors finding the same entity at the same range) are
    collapsed, so audit counts are not doubled.  A failing detector fails the
    whole detection rather than silently skipping its spans.
    """

    def __init__(self, detectors: list[Detector]) -> None:
        self._detectors = detectors
        self.name = "+".join(d.name for d in detectors)

    async def detect(self, text: str, context: Context) -> Detection:
        results = await asyncio.gather(*(d.detect(text, context) for d in self._detectors))
        unique: dict[tuple[int, int, str], Span] = {}
        for result in results:
            for span in result.spans:
                unique.setdefault((span.start, span.end, span.entity_type), span)
        return Detection(spans=list(unique.values()), cacheable=all(r.cacheable for r in results))
