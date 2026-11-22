"""Example detector plugin: vehicle license plates.

Demonstrates the Privyx plugin contract — subclass a kind's base class, set a
class-level ``name``, and add the class in ``setup(app)``.  Privyx calls it
when the file's directory is listed under ``plugins.paths``::

    # config.yaml
    plugins:
      paths: [./plugins]
    detectors:
      license_plate: {}      # runs after the built-in regex detector

Then ``privyx doctor`` lists it under plugins and the engine runs it with the
other detectors.  See ../../docs/development/plugins.md.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from privyx.core import Context, Detection
from privyx.detectors import Detector

if TYPE_CHECKING:
    from privyx import Privyx

#: Loose plate shape: 1–3 letters, an optional separator, 1–4 digits, and an
#: optional letter suffix — e.g. "B 1234 XYZ" or "ABC-123".  Illustrative only.
_PLATE = re.compile(r"\b[A-Z]{1,3}[\s-]?\d{1,4}(?:[\s-]?[A-Z]{1,3})?\b")


class LicensePlateDetector(Detector):
    """Detects license plates and labels them ``LICENSE_PLATE``."""

    name = "license_plate"

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = Detection()
        for match in _PLATE.finditer(text):
            detection.add(match.start(), match.end(), "LICENSE_PLATE", match.group())
        return detection


def setup(app: Privyx) -> None:
    """Add the detector to ``app``: what ``plugins.paths`` calls."""
    app.add(LicensePlateDetector)
