"""Example detector plugin: vehicle license plates.

Demonstrates the Privyx plugin contract — subclass a component base class and
set a class-level ``name``.  Privyx discovers this class when its directory is
listed under ``plugins.paths``::

    # config.yaml
    plugins:
      paths: [./plugins]
    detector:
      type: license_plate

Then ``privyx doctor`` lists it under plugins and the engine uses it as the
configured detector.  See ../../docs/development/plugins.md.
"""

from __future__ import annotations

import re

from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector

#: Loose plate shape: 1–3 letters, an optional separator, 1–4 digits, and an
#: optional letter suffix — e.g. "B 1234 XYZ" or "ABC-123".  Illustrative only.
_PLATE = re.compile(r"\b[A-Z]{1,3}[\s-]?\d{1,4}(?:[\s-]?[A-Z]{1,3})?\b")


class LicensePlateDetector(BaseDetector):
    """Detects license plates and labels them ``LICENSE_PLATE``."""

    name = "license_plate"

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = Detection()
        for match in _PLATE.finditer(text):
            detection.add(match.start(), match.end(), "LICENSE_PLATE", match.group())
        return detection
