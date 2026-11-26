"""Example detector plugin: IBANs, told apart from look-alikes by their checksum.

A regular expression can say what an IBAN looks like.  Whether a string of that
shape is one takes a calculation, which is what a plugin is for::

    # config.yaml
    plugins:
      paths: [./plugins]
    detectors:
      regex:
        disable: [IBAN]        # the built-in this plugin stands in for
      iban:                    # runs after the built-in regex detector
        countries: [DE, NL]    # optional: only these country codes

See ../../docs/tutorials/detector-plugin.md for a walkthrough.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import Field

from privyx.core import Context, Detection
from privyx.detectors import Detector

if TYPE_CHECKING:
    from privyx import Privyx

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


class IbanDetector(Detector):
    """Detects IBANs with a correct checksum and labels them ``IBAN``.

    ``countries`` are the country codes to accept; empty accepts every country.
    """

    name = "iban"

    class Options(Detector.Options):
        countries: list[str] = Field(default_factory=list)

    options: Options

    def detect_sync(self, text: str, context: Context) -> Detection:
        countries = {country.upper() for country in self.options.countries}
        detection = Detection()
        for match in _CANDIDATE.finditer(text):
            iban = _iban_in(match.group())
            if iban is None:
                continue
            if countries and iban[:2] not in countries:
                continue
            detection.add(match.start(), match.start() + len(iban), "IBAN", iban)
        return detection


def setup(app: Privyx) -> None:
    """Add the detector to ``app``: what ``plugins.paths`` calls."""
    app.add(IbanDetector)
