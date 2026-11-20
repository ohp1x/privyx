"""Default policy — pass through all detections unchanged."""

from __future__ import annotations

from privyx.core import Context, Detection
from privyx.policies._base import Policy


class DefaultPolicy(Policy):
    """Passes all detections through unchanged."""

    name = "default"

    def decide_sync(self, detection: Detection, context: Context) -> Detection:
        return detection
