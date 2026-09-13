"""Built-in detectors shipped with Privyx.

These are intentionally dependency-free: pure regex + simple heuristics.
"""

from __future__ import annotations

import re

from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector

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


class RegexDetector(BaseDetector):
    """Detect entities with a set of named regular expressions."""

    name = "regex"

    def __init__(self, patterns: dict[str, str] | None = None) -> None:
        super().__init__()
        patterns = patterns or dict(DEFAULT_PATTERNS)
        self._compiled = [(name, re.compile(pattern)) for name, pattern in patterns.items()]

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = Detection()
        for entity, pattern in self._compiled:
            for match in pattern.finditer(text):
                detection.add(match.start(), match.end(), entity, match.group())
        return detection


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
        self._compiled = [
            (entity, re.compile(pattern)) for entity, pattern in self._rules.items()
        ]

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = Detection()
        for entity, pattern in self._compiled:
            for match in pattern.finditer(text):
                detection.add(match.start(), match.end(), entity, match.group())
        return detection
