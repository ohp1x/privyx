"""Example detector plugin: IBANs, told apart from look-alikes by their checksum.

A regular expression can say what an IBAN looks like.  Whether a string of that
shape is one takes a calculation, which is what a plugin is for::

    # config.yaml
    plugins:
      paths: [./plugins]
    detector:
      - type: regex            # the built-in patterns
      - type: iban
        countries: [DE, NL]    # optional: only these country codes

See ../../docs/tutorials/detector-plugin.md for a walkthrough.
"""

from __future__ import annotations

import re
from typing import Any, Self

from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector

#: A country code, two check digits, then the account part: 11 to 30 letters
#: or digits, written solid or in groups of four.
_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")

#: The shortest IBAN in use (Norway).
_MIN_LENGTH = 15


def _valid(iban: str) -> bool:
    """Whether ``iban`` passes the ISO 13616 check (mod 97 equals 1)."""
    compact = iban.replace(" ", "")
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _iban_in(candidate: str) -> str | None:
    """The valid IBAN ``candidate`` starts with, if any.

    The pattern is greedy, so a group that follows the number (``EUR``, a
    reference) can end up in the candidate.  Dropping trailing groups one at a
    time finds the number without it.
    """
    while len(candidate.replace(" ", "")) >= _MIN_LENGTH:
        if _valid(candidate):
            return candidate
        candidate = candidate.rpartition(" ")[0]
    return None


class IbanDetector(BaseDetector):
    """Detects IBANs with a correct checksum and labels them ``IBAN``.

    Args:
        countries: Country codes to accept; empty accepts every country.
    """

    name = "iban"

    def __init__(self, countries: list[str] | None = None) -> None:
        super().__init__()
        self._countries = {country.upper() for country in countries or []}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Self:
        return cls(countries=config.get("countries"))

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = Detection()
        for match in _CANDIDATE.finditer(text):
            iban = _iban_in(match.group())
            if iban is None:
                continue
            if self._countries and iban[:2] not in self._countries:
                continue
            detection.add(match.start(), match.start() + len(iban), "IBAN", iban)
        return detection
